//! Reaching the peer's backend as a client, and starting the daemon that
//! serves it when nothing is.
//!
//! An embedded SQLite backend belongs to one process, so exactly one process
//! may open a given state directory: `chaz daemon`. Everything else connects to
//! it over the eidetica service socket. That leaves one question — what a
//! frontend does when it finds no daemon running — and the answer is *not*
//! "open the database itself". It starts a daemon and connects to that.
//!
//! Deliberately, nothing in this module can open a backend. The only way to
//! reach the data from here is a socket, which is what makes the sole-opener
//! rule structural rather than a convention someone has to remember.
//!
//! Two claims, with two different lifetimes, keep that true:
//!
//! - **The daemon claim** (`daemon.lock`) is held by the running daemon for its
//!   whole life. A second daemon fails to take it and refuses to start, so a
//!   state directory can never have two openers even for an instant.
//! - **The start claim** (`daemon-start.lock`) is held by whichever *client* is
//!   currently starting a daemon, and released once that daemon is serving. It
//!   is what makes N frontends racing on a cold state directory produce one
//!   daemon rather than N: exactly one takes the claim and starts, and the
//!   losers wait for the winner's socket instead of starting their own.
//!
//! Both are `flock`, which the kernel releases when the holder dies. A crashed
//! daemon therefore leaves no claim to clean up — only a dead socket file,
//! which is treated as no socket at all.

use anyhow::{Context, Result};
use std::fs::File;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

/// How long a client waits for a daemon it started to accept connections
/// before giving up. Bounded on purpose: a daemon that never comes up must
/// surface as an error, not as a hang.
pub const DEFAULT_READINESS_TIMEOUT: Duration = Duration::from_secs(30);

/// How often readiness is re-probed while waiting.
pub const DEFAULT_POLL_INTERVAL: Duration = Duration::from_millis(50);

/// Name of the claim the running daemon holds for its lifetime, inside the
/// state directory.
pub const DAEMON_LOCK_FILE: &str = "daemon.lock";

/// Name of the claim a client holds while it starts a daemon, inside the state
/// directory.
pub const START_LOCK_FILE: &str = "daemon-start.lock";

/// Is something listening on `path` right now?
///
/// A successful connect is the only trustworthy answer. The socket file
/// existing is not: a daemon that died leaves the file behind, and treating
/// that as "a daemon is running" is how a frontend ends up waiting forever for
/// a peer that no longer exists.
pub async fn socket_is_ready(path: &Path) -> bool {
    let mode_0600 = tokio::fs::metadata(path)
        .await
        .map(|m| m.permissions().mode() & 0o777 == 0o600)
        .unwrap_or(false);
    mode_0600 && tokio::net::UnixStream::connect(path).await.is_ok()
}

/// Starts a daemon. Production spawns the chaz binary; tests substitute
/// something cheaper, which is the only way to exercise the race without
/// standing up real peers.
pub trait DaemonSpawn {
    /// Start a daemon and return once it has been *launched* — not once it is
    /// ready. Readiness is the caller's to wait for, because it is observed
    /// through the socket rather than reported by the child.
    fn spawn(&self) -> Result<()>;
}

/// Spawns `chaz --config <path> daemon` as a detached child.
pub struct SpawnChazDaemon {
    /// The chaz binary to run. Defaults to the running executable.
    pub program: PathBuf,
    /// Config the daemon should read. A client and the daemon it starts must
    /// agree on the state directory, and the config path is what carries that.
    pub config_path: PathBuf,
}

impl SpawnChazDaemon {
    /// Start the daemon from the currently running executable, so a client
    /// never starts a chaz other than itself.
    pub fn from_current_exe(config_path: PathBuf) -> Result<Self> {
        Ok(Self {
            program: std::env::current_exe()
                .context("could not determine the running chaz executable")?,
            config_path,
        })
    }
}

impl DaemonSpawn for SpawnChazDaemon {
    fn spawn(&self) -> Result<()> {
        // The daemon outlives the client that started it — that is the point of
        // "persistent". Its stdio is dropped rather than inherited: a daemon
        // writing into the client's stdout would corrupt output the client
        // reserves for its own result.
        let mut command = std::process::Command::new(&self.program);
        command
            .arg("--config")
            .arg(&self.config_path)
            .arg("daemon")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            // Detached daemons have no terminal, so line-buffered stdout can
            // otherwise stay invisible until process exit. This is also a
            // useful opt-in diagnostic for integration harnesses.
            .env("CHAZ_DAEMON_DETACHED", "1");
        // SAFETY: this hook calls only async-signal-safe `setsid(2)` between
        // fork and exec. A separate session keeps a daemon launched by a
        // one-shot frontend alive after the frontend's process group exits.
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        command.spawn().with_context(|| {
            format!(
                "could not start a chaz daemon from {}",
                self.program.display()
            )
        })?;
        Ok(())
    }
}

/// Connect one independent client and create a database through it. Used by
/// the live service test to prove server→client write notifications are not
/// merely visible after a later poll.
#[cfg(test)]
async fn create_named_database(socket: &Path, name: &str) -> Result<()> {
    let url_path = resolve_socket_url_path(socket)?;
    let instance = eidetica::Instance::connect(format!("unix://{}", url_path.display())).await?;
    let mut user = instance.login_user("chaz", None).await?;
    let key = user.get_default_key()?;
    let mut settings = eidetica::crdt::Doc::new();
    settings.set("name", name);
    user.create_database(settings, &key).await?;
    Ok(())
}

/// Resolve the socket path to the absolute form eidetica's `unix://` URL
/// parser requires.
///
/// `resolve_service_socket` resolves configured relative paths against the
/// state directory before constructing an `AutoStart`, so the fallback here is
/// only for the state-directory-unavailable default socket.
fn resolve_socket_url_path(socket: &Path) -> Result<PathBuf> {
    if socket.is_absolute() {
        return Ok(socket.to_path_buf());
    }
    let cwd = std::env::current_dir().with_context(|| {
        format!(
            "could not resolve the relative service socket {} against the working directory",
            socket.display()
        )
    })?;
    Ok(cwd.join(socket))
}

/// Finds the daemon serving a state directory, starting one if there is none.
pub struct AutoStart<S> {
    /// Where the daemon serves its eidetica Instance.
    socket: PathBuf,
    /// The start claim's path — see the module docs.
    start_lock: PathBuf,
    spawn: S,
    readiness_timeout: Duration,
    poll_interval: Duration,
}

impl<S: DaemonSpawn> AutoStart<S> {
    /// `socket` is the service socket; `state_dir` is where the claim files
    /// live, which is the same directory in every real deployment.
    pub fn new(socket: PathBuf, state_dir: &Path, spawn: S) -> Self {
        Self {
            socket,
            start_lock: state_dir.join(START_LOCK_FILE),
            spawn,
            readiness_timeout: DEFAULT_READINESS_TIMEOUT,
            poll_interval: DEFAULT_POLL_INTERVAL,
        }
    }

    /// Override the readiness bound. Mostly for tests, which cannot afford to
    /// wait out the production timeout to prove it exists.
    #[cfg(test)]
    pub fn with_readiness_timeout(mut self, timeout: Duration) -> Self {
        self.readiness_timeout = timeout;
        self
    }

    /// Override the readiness poll interval.
    #[cfg(test)]
    pub fn with_poll_interval(mut self, interval: Duration) -> Self {
        self.poll_interval = interval;
        self
    }

    /// Return once a daemon is serving the socket, starting one if needed.
    ///
    /// Every failure path is an error. None of them opens the backend, because
    /// nothing here can.
    pub async fn ensure_serving(&self) -> Result<()> {
        if socket_is_ready(&self.socket).await {
            return Ok(());
        }

        // Nothing is serving. Exactly one contender may act on that, so decide
        // who by an atomic claim rather than by who looked first.
        let lock = File::create(&self.start_lock).with_context(|| {
            format!(
                "could not open the daemon start claim at {}",
                self.start_lock.display()
            )
        })?;

        match lock.try_lock() {
            Ok(()) => {
                // Won the claim. Re-probe: a daemon may have finished coming up
                // between the first probe and the claim, in which case starting
                // a second one would be exactly the bug this prevents.
                if !socket_is_ready(&self.socket).await {
                    match (
                        self.claim_absent_daemon()?,
                        socket_is_ready(&self.socket).await,
                    ) {
                        (Some(daemon_claim), false) => {
                            // Taking the daemon claim excludes a daemon from
                            // binding while stale cleanup runs. It also lets a
                            // manually started daemon that has bound but not
                            // chmodded its socket keep ownership: it holds the
                            // claim, so this branch never unlinks its socket.
                            self.clear_stale_socket()?;
                            // The child daemon must hold this claim for its
                            // lifetime, not its launcher.
                            drop(daemon_claim);
                            self.spawn.spawn()?;
                        }
                        (None, _) => {
                            // A daemon owns the state directory and may be in
                            // the bind-before-chmod window. Wait within the
                            // normal bound; never unlink its socket or spawn a
                            // competing backend opener.
                        }
                        (Some(_), true) => {}
                    }
                }
                let ready = self.await_ready().await;
                // Released whether or not it came up: holding a claim over a
                // failed start would wedge every later invocation too.
                drop(lock);
                ready
            }
            Err(std::fs::TryLockError::WouldBlock) => {
                // Lost. The winner is starting a daemon; waiting for its socket
                // is the whole of our job. Starting one here is what turns a
                // race into two openers.
                drop(lock);
                self.await_ready().await
            }
            Err(std::fs::TryLockError::Error(e)) => Err(anyhow::Error::new(e).context(format!(
                "could not take the daemon start claim at {}",
                self.start_lock.display()
            ))),
        }
    }

    /// Connect to the daemon serving this peer, starting one if there is none.
    pub async fn connect(&self) -> Result<eidetica::Instance> {
        self.ensure_serving().await?;
        let url_path = resolve_socket_url_path(&self.socket)?;
        eidetica::Instance::connect(format!("unix://{}", url_path.display()))
            .await
            .with_context(|| format!("could not connect to {}", self.socket.display()))
    }

    /// Remove a socket file nothing is listening on.
    ///
    /// Only ever reached while holding the start claim and after a failed
    /// connect, so the file is a crash leftover by elimination. Eidetica's
    /// service server would unlink it anyway; doing it here keeps the "stale
    /// socket means absent" rule in one place.
    fn clear_stale_socket(&self) -> Result<()> {
        match std::fs::remove_file(&self.socket) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(anyhow::Error::new(e).context(format!(
                "could not clear the stale socket at {}",
                self.socket.display()
            ))),
        }
    }

    /// Atomically determine whether a daemon owns this state directory.
    ///
    /// A returned claim is held through stale-socket cleanup, so no daemon can
    /// bind the socket between deciding it is dead and unlinking it. `None`
    /// means a daemon is alive; callers must wait for it rather than touch its
    /// socket.
    fn claim_absent_daemon(&self) -> Result<Option<File>> {
        let path = self
            .start_lock
            .parent()
            .expect("start lock always has a state-directory parent")
            .join(DAEMON_LOCK_FILE);
        let file = File::create(&path)
            .with_context(|| format!("could not open the daemon claim at {}", path.display()))?;
        match file.try_lock() {
            Ok(()) => Ok(Some(file)),
            Err(std::fs::TryLockError::WouldBlock) => Ok(None),
            Err(std::fs::TryLockError::Error(e)) => Err(anyhow::Error::new(e).context(format!(
                "could not inspect the daemon claim at {}",
                path.display()
            ))),
        }
    }

    /// Poll until the socket accepts connections, or the bound expires.
    async fn await_ready(&self) -> Result<()> {
        let deadline = Instant::now() + self.readiness_timeout;
        loop {
            if socket_is_ready(&self.socket).await {
                return Ok(());
            }
            if Instant::now() >= deadline {
                anyhow::bail!(
                    "no chaz daemon accepted connections on {} within {:?}",
                    self.socket.display(),
                    self.readiness_timeout
                );
            }
            tokio::time::sleep(self.poll_interval).await;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    /// A stand-in daemon: binds the socket after `delay`, counts its starts,
    /// and holds the listener open for the test's lifetime.
    struct FakeDaemon {
        socket: PathBuf,
        delay: Duration,
        starts: Arc<AtomicUsize>,
        listeners: Arc<std::sync::Mutex<Vec<std::os::unix::net::UnixListener>>>,
    }

    impl FakeDaemon {
        fn new(socket: &Path) -> Self {
            Self {
                socket: socket.to_path_buf(),
                delay: Duration::ZERO,
                starts: Arc::new(AtomicUsize::new(0)),
                listeners: Arc::new(std::sync::Mutex::new(Vec::new())),
            }
        }

        fn with_delay(mut self, delay: Duration) -> Self {
            self.delay = delay;
            self
        }

        fn starts(&self) -> Arc<AtomicUsize> {
            Arc::clone(&self.starts)
        }
    }

    impl DaemonSpawn for FakeDaemon {
        fn spawn(&self) -> Result<()> {
            self.starts.fetch_add(1, Ordering::SeqCst);
            let socket = self.socket.clone();
            let delay = self.delay;
            let listeners = Arc::clone(&self.listeners);
            std::thread::spawn(move || {
                std::thread::sleep(delay);
                if let Ok(listener) = std::os::unix::net::UnixListener::bind(&socket) {
                    std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o600))
                        .unwrap();
                    listeners.lock().unwrap().push(listener);
                }
            });
            Ok(())
        }
    }

    /// A daemon that never comes up, so readiness has to be the thing that ends
    /// the wait.
    struct NeverReady {
        starts: Arc<AtomicUsize>,
    }

    impl DaemonSpawn for NeverReady {
        fn spawn(&self) -> Result<()> {
            self.starts.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }
    }

    /// A daemon that cannot be started at all.
    struct SpawnFails;

    impl DaemonSpawn for SpawnFails {
        fn spawn(&self) -> Result<()> {
            anyhow::bail!("no such binary")
        }
    }

    fn fast<S: DaemonSpawn>(socket: PathBuf, state_dir: &Path, spawn: S) -> AutoStart<S> {
        AutoStart::new(socket, state_dir, spawn)
            .with_readiness_timeout(Duration::from_secs(5))
            .with_poll_interval(Duration::from_millis(5))
    }

    async fn start_service() -> (
        tempfile::TempDir,
        PathBuf,
        tokio::sync::watch::Sender<()>,
        tokio::task::JoinHandle<eidetica::Result<()>>,
    ) {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("eidetica.sock");
        let (instance, _) = eidetica::Instance::create_backend(
            Box::new(eidetica::backend::database::InMemory::new()),
            eidetica::NewUser::passwordless("chaz"),
        )
        .await
        .unwrap();
        let (stop, stopped) = tokio::sync::watch::channel(());
        let server = eidetica::service::ServiceServer::new(instance, socket.clone());
        let task = tokio::spawn(async move { server.run(stopped).await });
        let auto = fast(
            socket.clone(),
            dir.path(),
            NeverReady {
                starts: Arc::new(AtomicUsize::new(0)),
            },
        );
        auto.await_ready().await.unwrap();
        (dir, socket, stop, task)
    }

    #[tokio::test]
    async fn readiness_requires_a_live_owner_only_socket() {
        let dir = tempfile::tempdir().unwrap();

        assert!(!socket_is_ready(&dir.path().join("missing.sock")).await);

        // A crash leftover: the path is there, nobody is behind it.
        let stale = dir.path().join("stale.sock");
        std::fs::write(&stale, b"").unwrap();
        assert!(!socket_is_ready(&stale).await);

        let live = dir.path().join("live.sock");
        let _listener = std::os::unix::net::UnixListener::bind(&live).unwrap();
        std::fs::set_permissions(&live, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(
            !socket_is_ready(&live).await,
            "a socket accepting connections before chmod must not be ready"
        );
        std::fs::set_permissions(&live, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(socket_is_ready(&live).await);
    }

    #[tokio::test]
    async fn a_live_socket_starts_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("eidetica.sock");
        let _listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
        std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o600)).unwrap();

        let daemon = FakeDaemon::new(&socket);
        let starts = daemon.starts();
        fast(socket.clone(), dir.path(), daemon)
            .ensure_serving()
            .await
            .expect("a live socket needs no daemon");
        assert_eq!(starts.load(Ordering::SeqCst), 0);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn live_clients_share_writes_and_notifications_bidirectionally() {
        let (_dir, socket, stop, task) = start_service().await;
        let auto = fast(
            socket.clone(),
            socket.parent().unwrap(),
            NeverReady {
                starts: Arc::new(AtomicUsize::new(0)),
            },
        );
        let instance_a = auto.connect().await.expect("first live client connects");
        let mut user_a = instance_a.login_user("chaz", None).await.unwrap();
        let preferences_a = user_a.user_database().clone();
        let (tx, mut rx) = tokio::sync::mpsc::channel(1);
        preferences_a
            .on_write(move |_event, _db| {
                let tx = tx.clone();
                Box::pin(async move {
                    let _ = tx.send(()).await;
                    Ok(())
                })
            })
            .await
            .unwrap()
            .detach();

        create_named_database(&socket, "from-client-b")
            .await
            .expect("second client writes through the service");
        tokio::time::timeout(Duration::from_secs(5), rx.recv())
            .await
            .expect("client A receives the daemon-published notification")
            .expect("notification channel stays open");
        let instance_after_b = auto.connect().await.expect("verification client connects");
        let user_after_b = instance_after_b.login_user("chaz", None).await.unwrap();
        assert_eq!(
            user_after_b
                .find_database("from-client-b")
                .await
                .expect("a fresh client sees client B's database")
                .len(),
            1
        );

        let key = user_a.get_default_key().unwrap();
        let mut settings = eidetica::crdt::Doc::new();
        settings.set("name", "from-client-a");
        user_a.create_database(settings, &key).await.unwrap();
        let instance_b = auto.connect().await.expect("third live client connects");
        let user_b = instance_b.login_user("chaz", None).await.unwrap();
        assert_eq!(
            user_b
                .find_database("from-client-a")
                .await
                .expect("client B sees client A's database")
                .len(),
            1
        );

        drop(stop);
        task.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn a_cold_state_dir_starts_exactly_one_daemon() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("eidetica.sock");

        let daemon = FakeDaemon::new(&socket);
        let starts = daemon.starts();
        // Held for the assertions: the fake daemon lives inside it, and a
        // dropped one takes its listener with it.
        let auto = fast(socket.clone(), dir.path(), daemon);
        auto.ensure_serving().await.expect("daemon should come up");
        assert_eq!(starts.load(Ordering::SeqCst), 1);
        std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(socket_is_ready(&socket).await);
    }

    #[tokio::test]
    async fn a_dead_socket_file_counts_as_no_daemon() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("eidetica.sock");
        // A crash leftover: the path exists, nothing is listening on it.
        std::fs::write(&socket, b"not a socket").unwrap();

        let daemon = FakeDaemon::new(&socket);
        let starts = daemon.starts();
        let auto = fast(socket.clone(), dir.path(), daemon);
        auto.ensure_serving()
            .await
            .expect("a stale socket must not block a start");
        assert_eq!(starts.load(Ordering::SeqCst), 1);
        std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(socket_is_ready(&socket).await);
    }

    #[tokio::test]
    async fn live_daemon_in_bind_before_chmod_window_keeps_its_socket() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("eidetica.sock");
        let daemon_lock = File::create(dir.path().join(DAEMON_LOCK_FILE)).unwrap();
        daemon_lock.try_lock().unwrap();
        let listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
        std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o755)).unwrap();

        let starts = Arc::new(AtomicUsize::new(0));
        let auto = fast(
            socket.clone(),
            dir.path(),
            NeverReady {
                starts: Arc::clone(&starts),
            },
        );
        let wait = tokio::spawn(async move { auto.ensure_serving().await });

        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(
            socket.exists(),
            "a live daemon's pre-chmod socket was unlinked"
        );
        assert_eq!(
            starts.load(Ordering::SeqCst),
            0,
            "the client must wait for the live daemon instead of starting another"
        );

        std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o600)).unwrap();
        wait.await
            .unwrap()
            .expect("the client waits through bind-before-chmod and then connects");
        drop(listener);
        drop(daemon_lock);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn racing_callers_converge_on_one_daemon() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("eidetica.sock");

        // Slow enough to come up that every contender is inside the race, which
        // is the case a fast daemon would hide.
        let daemon = Arc::new(FakeDaemon::new(&socket).with_delay(Duration::from_millis(150)));
        let starts = daemon.starts();

        struct Shared(Arc<FakeDaemon>);
        impl DaemonSpawn for Shared {
            fn spawn(&self) -> Result<()> {
                self.0.spawn()
            }
        }

        let mut callers = tokio::task::JoinSet::new();
        for _ in 0..8 {
            let socket = socket.clone();
            let state_dir = dir.path().to_path_buf();
            let daemon = Arc::clone(&daemon);
            callers.spawn(async move {
                fast(socket, &state_dir, Shared(daemon))
                    .ensure_serving()
                    .await
            });
        }

        let mut connected = 0;
        while let Some(result) = callers.join_next().await {
            result
                .unwrap()
                .expect("every caller reaches the one daemon");
            connected += 1;
        }
        assert_eq!(connected, 8);
        assert_eq!(
            starts.load(Ordering::SeqCst),
            1,
            "exactly one contender may start a daemon"
        );
    }

    #[tokio::test]
    async fn readiness_is_bounded() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("eidetica.sock");

        let starts = Arc::new(AtomicUsize::new(0));
        let err = AutoStart::new(
            socket.clone(),
            dir.path(),
            NeverReady {
                starts: Arc::clone(&starts),
            },
        )
        .with_readiness_timeout(Duration::from_millis(100))
        .with_poll_interval(Duration::from_millis(5))
        .ensure_serving()
        .await
        .expect_err("a daemon that never binds must not hang the caller");

        assert!(
            format!("{err}").contains("accepted connections"),
            "unhelpful readiness error: {err}"
        );
        assert_eq!(starts.load(Ordering::SeqCst), 1);
        // The state directory is left for the next invocation to retry: no
        // socket invented, no database touched.
        assert!(!socket.exists());
    }

    #[tokio::test]
    async fn a_failed_start_surfaces_rather_than_falling_back() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("eidetica.sock");

        let err = fast(socket.clone(), dir.path(), SpawnFails)
            .ensure_serving()
            .await
            .expect_err("a start that failed is an error, not a direct open");
        assert!(format!("{err}").contains("no such binary"), "{err}");
        assert!(!socket.exists());
    }

    #[tokio::test]
    async fn a_failed_start_does_not_wedge_the_next_caller() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("eidetica.sock");

        fast(socket.clone(), dir.path(), SpawnFails)
            .ensure_serving()
            .await
            .expect_err("first caller fails to start a daemon");

        // The start claim was released, so the next caller may take it.
        let daemon = FakeDaemon::new(&socket);
        let starts = daemon.starts();
        fast(socket.clone(), dir.path(), daemon)
            .ensure_serving()
            .await
            .expect("a later caller retries the same path");
        assert_eq!(starts.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn an_absolute_socket_stays_as_is_for_the_connection_url() {
        let socket = Path::new("/run/chaz/eidetica.sock");
        assert_eq!(
            resolve_socket_url_path(socket).expect("an absolute path resolves as-is"),
            PathBuf::from("/run/chaz/eidetica.sock")
        );
    }

    #[test]
    fn a_relative_socket_resolves_against_the_working_directory_for_the_url() {
        // No cwd mutation needed: the resolver's output for a relative path is
        // by construction the current directory joined with it, and the
        // process cwd is stable while this test runs.
        let socket = Path::new("eidetica.sock");
        let expected = std::env::current_dir()
            .expect("the test harness has a working directory")
            .join("eidetica.sock");
        assert_eq!(
            resolve_socket_url_path(socket).expect("a relative path resolves against the cwd"),
            expected
        );
    }
}
