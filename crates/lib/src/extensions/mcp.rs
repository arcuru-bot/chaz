//! MCP-server-as-extension — one `McpExtension` per configured MCP server.
//!
//! Each instance wraps an [`McpServer`] and contributes its discovered
//! tools through `ExtensionInstance::tools`. Tools carry attribution
//! (`owner: "mcp-<server_name>"`) so they participate in per-session
//! extension filtering, the same as any built-in extension.
//!
//! Failed servers are logged and produce zero tools (matching the legacy
//! `start_mcp_servers` resilience contract).

use crate::config::McpServerConfig;
use crate::extension::instance::{ExtensionInstance, InstantiateFuture, ScopeCtx};
use crate::extension::manifest::ExtensionManifest;
use crate::extension::{Extension, ExtensionRef, HookKind};
use crate::mcp::server::{McpServer, build_capability_tools};
use crate::tool::Tool;
use std::sync::Arc;
use tracing::{info, warn};

/// An MCP server wrapped as an extension.
pub struct McpExtension {
    /// Leaked extension name — the `Extension` trait requires `&'static str`,
    /// and MCP extensions live for the process lifetime anyway.
    name: &'static str,
    /// Frozen copy of the server config.
    config: McpServerConfig,
}

impl McpExtension {
    pub fn new(config: McpServerConfig) -> Self {
        let name: &'static str = Box::leak(format!("mcp-{}", config.name).into_boxed_str());
        Self { name, config }
    }
}

impl Extension for McpExtension {
    fn name(&self) -> &'static str {
        self.name
    }

    fn supported_hooks(&self) -> &[HookKind] {
        &[HookKind::Tool]
    }

    fn manifest(&self) -> ExtensionManifest {
        ExtensionManifest {
            name: self.name.to_string(),
            extension_ref: ExtensionRef::builtin(self.name),
            supported_hooks: vec![HookKind::Tool],
            required_capabilities: Vec::new(),
            requested_capabilities: Vec::new(),
            provides_capabilities: Vec::new(),
        }
    }

    fn instantiate<'a>(&'a self, scope_ctx: ScopeCtx<'a>) -> InstantiateFuture<'a> {
        let manifest = self.manifest();
        let config = self.config.clone();
        let name = self.name;
        let mcp_registry = scope_ctx.peer().mcp_registry.clone();
        let tool_registry = scope_ctx.peer().tool_registry.clone();
        Box::pin(async move {
            // Return immediately with an empty-tools instance. MCP server
            // startup runs in the background and hot-adds tools when ready.
            let instance = Arc::new(McpInstance {
                manifest,
                _name: name,
                tools: Vec::new(),
            });

            // Spawn the actual MCP server startup off the critical path.
            let owner: &'static str = Box::leak(
                format!("mcp-{}", config.name).into_boxed_str(),
            );
            tokio::spawn(async move {
                match McpServer::start(&config).await {
                    Ok(server) => {
                        let server = Arc::new(server);
                        mcp_registry.insert_running(config.name.clone(), server.clone());
                        let capability_tools = build_capability_tools(server.clone(), &config.name);
                        match server.discover_and_wrap_tools(&config.name).await {
                            Ok(t) => {
                                let count = t.len();
                                let cap_count = capability_tools.len();
                                info!(
                                    server = %config.name,
                                    tools = count,
                                    capability_tools = cap_count,
                                    "MCP server tools discovered (async)"
                                );
                                for tool in t {
                                    tool_registry.register_arc_owned(
                                        Arc::new(tool) as Arc<dyn Tool>,
                                        Some(owner),
                                    );
                                }
                                for tool in capability_tools {
                                    tool_registry.register_arc_owned(tool, Some(owner));
                                }
                            }
                            Err(e) => {
                                warn!(
                                    server = %config.name,
                                    error = %e,
                                    "MCP server tool discovery failed (async) — skipping"
                                );
                                for tool in capability_tools {
                                    tool_registry.register_arc_owned(tool, Some(owner));
                                }
                            }
                        }
                    }
                    Err(e) => {
                        warn!(
                            server = %config.name,
                            error = %e,
                            "MCP server failed to start (async) — skipping its tools"
                        );
                        mcp_registry.insert_failed(config.name.clone(), e);
                    }
                }
            });

            Ok(instance as Arc<dyn ExtensionInstance>)
        })
    }
}

struct McpInstance {
    manifest: ExtensionManifest,
    _name: &'static str,
    tools: Vec<Arc<dyn Tool>>,
}

impl ExtensionInstance for McpInstance {
    fn manifest(&self) -> &ExtensionManifest {
        &self.manifest
    }

    fn tools(&self) -> Vec<Arc<dyn Tool>> {
        self.tools.clone()
    }
}
