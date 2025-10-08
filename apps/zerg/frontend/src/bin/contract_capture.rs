//! Generate (or refresh) the Pact contract file that the backend provider
//! verification test consumes.  This binary is **not** used by the browser
//! build – it runs on the host via `cargo run -p agent-platform-frontend --bin contract_capture`.

use std::fs;
use std::path::PathBuf;

use serde_json::json;

fn main() {
    // ------------------------------------------------------------------
    // 1. Build Pact JSON instance in-memory --------------------------------
    // ------------------------------------------------------------------
    let pact_json = json!({
        "consumer": { "name": "agent-frontend" },
        "provider": { "name": "zerg-backend" },
        "interactions": [
            // Agent lifecycle events
            {
                "description": "agent created → receive agent_event with creation data",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["agent:123"],
                        "message_id": "test-agent-1"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "agent_event",
                        "topic": "agent:123",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "id": 123,
                            "status": "idle",
                            "last_run_at": null,
                            "next_run_at": null,
                            "last_error": null
                        }
                    }
                }
            },
            {
                "description": "agent updated → receive agent_updated with status change",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["agent:123"],
                        "message_id": "test-agent-2"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "agent_updated",
                        "topic": "agent:123",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "id": 123,
                            "status": "running",
                            "last_run_at": "2023-01-01T00:00:00Z",
                            "next_run_at": null,
                            "last_error": null
                        }
                    }
                }
            },
            {
                "description": "agent deleted → receive agent_deleted notification",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["agent:123"],
                        "message_id": "test-agent-3"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "agent_deleted",
                        "topic": "agent:123",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "id": 123
                        }
                    }
                }
            },
            {
                "description": "agent state update → receive agent_state message",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["agent:123"],
                        "message_id": "test-agent-4"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "agent_state",
                        "topic": "agent:123",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "id": 123,
                            "status": "idle",
                            "last_run_at": "2023-01-01T00:00:00Z",
                            "next_run_at": "2023-01-01T01:00:00Z",
                            "last_error": null
                        }
                    }
                }
            },
            {
                "description": "agent created event → receive agent_created message",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["agent:123"],
                        "message_id": "test-agent-5"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "agent_created",
                        "topic": "agent:123",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "id": 123,
                            "status": "idle",
                            "last_run_at": null,
                            "next_run_at": null,
                            "last_error": null
                        }
                    }
                }
            },
            // Run updates
            {
                "description": "agent run status → receive run_update with progress",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["agent:123"],
                        "message_id": "test-run-1"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "run_update",
                        "topic": "agent:123",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "id": 456,
                            "agent_id": 123,
                            "thread_id": 789,
                            "status": "running",
                            "trigger": "manual",
                            "started_at": "2023-01-01T00:00:00Z",
                            "finished_at": null,
                            "duration_ms": null,
                            "error": null
                        }
                    }
                }
            },
            // Thread events
            {
                "description": "thread created → receive thread_created event",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["thread:789"],
                        "message_id": "test-thread-1"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "thread_created",
                        "topic": "thread:789",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "thread_id": 789,
                            "agent_id": 123
                        }
                    }
                }
            },
            {
                "description": "thread updated → receive thread_updated event",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["thread:789"],
                        "message_id": "test-thread-2"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "thread_updated",
                        "topic": "thread:789",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "thread_id": 789,
                            "agent_id": 123
                        }
                    }
                }
            },
            {
                "description": "thread deleted → receive thread_deleted event",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["thread:789"],
                        "message_id": "test-thread-3"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "thread_deleted",
                        "topic": "thread:789",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "thread_id": 789,
                            "agent_id": 123
                        }
                    }
                }
            },
            {
                "description": "generic thread event → receive thread_event",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["thread:789"],
                        "message_id": "test-thread-4"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "thread_event",
                        "topic": "thread:789",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "thread_id": 789,
                            "agent_id": 123
                        }
                    }
                }
            },
            // Thread messages
            {
                "description": "new thread message → receive thread_message",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["thread:789"],
                        "message_id": "test-msg-1"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "thread_message",
                        "topic": "thread:789",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "thread_id": 789,
                            "message": {
                                "id": 101,
                                "thread_id": 789,
                                "role": "user",
                                "content": "Hello",
                                "created_at": "2023-01-01T00:00:00Z",
                                "updated_at": "2023-01-01T00:00:00Z"
                            }
                        }
                    }
                }
            },
            {
                "description": "thread message created → receive thread_message_created",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["thread:789"],
                        "message_id": "test-msg-2"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "thread_message_created",
                        "topic": "thread:789",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "thread_id": 789,
                            "message": {
                                "id": 102,
                                "thread_id": 789,
                                "role": "assistant",
                                "content": "Hi there!",
                                "created_at": "2023-01-01T00:01:00Z",
                                "updated_at": "2023-01-01T00:01:00Z"
                            }
                        }
                    }
                }
            },
            // Streaming events
            {
                "description": "streaming starts → receive stream_start",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["thread:789"],
                        "message_id": "test-stream-1"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "stream_start",
                        "topic": "thread:789",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "thread_id": 789
                        }
                    }
                }
            },
            {
                "description": "streaming chunk → receive stream_chunk",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["thread:789"],
                        "message_id": "test-stream-2"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "stream_chunk",
                        "topic": "thread:789",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "thread_id": 789,
                            "chunk_type": "assistant_token",
                            "content": "Hello"
                        }
                    }
                }
            },
            {
                "description": "streaming ends → receive stream_end",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["thread:789"],
                        "message_id": "test-stream-3"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "stream_end",
                        "topic": "thread:789",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "thread_id": 789
                        }
                    }
                }
            },
            // Assistant ID
            {
                "description": "assistant message ID → receive assistant_id",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["thread:789"],
                        "message_id": "test-assistant-1"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "assistant_id",
                        "topic": "thread:789",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "thread_id": 789,
                            "message_id": 102
                        }
                    }
                }
            },
            // User updates
            {
                "description": "user profile updated → receive user_update",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["user:1"],
                        "message_id": "test-user-1"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "user_update",
                        "topic": "user:1",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "id": 1,
                            "email": "user@example.com",
                            "display_name": "Test User",
                            "avatar_url": "https://example.com/avatar.png"
                        }
                    }
                }
            },
            // Workflow execution events
            {
                "description": "workflow node state → receive node_state",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["workflow_execution:123"],
                        "message_id": "test-node-1"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "node_state",
                        "topic": "workflow_execution:123",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "execution_id": 123,
                            "node_id": "node-1",
                            "status": "running",
                            "output": null,
                            "error": null
                        }
                    }
                }
            },
            {
                "description": "workflow execution finished → receive execution_finished",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["workflow_execution:123"],
                        "message_id": "test-workflow-1"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "execution_finished",
                        "topic": "workflow_execution:123",
                        "req_id": "test-workflow-1",
                        "ts": 1234567890,
                        "data": {
                            "execution_id": 123,
                            "status": "success",
                            "error": null,
                            "duration_ms": 1500
                        }
                    }
                }
            },
            {
                "description": "workflow node log → receive node_log",
                "request": {
                    "type": "websocket",
                    "subtype": "text",
                    "body": {
                        "type": "subscribe",
                        "topics": ["workflow_execution:123"],
                        "message_id": "test-log-1"
                    }
                },
                "response": {
                    "body": {
                        "v": 1,
                        "type": "node_log",
                        "topic": "workflow_execution:123",
                        "req_id": null,
                        "ts": 1234567890,
                        "data": {
                            "execution_id": 123,
                            "node_id": "node-1",
                            "stream": "stdout",
                            "text": "Processing data..."
                        }
                    }
                }
            }
        ],
        "metadata": {
            "pactSpecification": { "version": "4.0" }
        }
    });

    // ------------------------------------------------------------------
    // 2. Resolve output path ------------------------------------------------
    // ------------------------------------------------------------------
    // The binary lives in <repo>/frontend/.  We want to write to
    // <repo>/contracts/frontend-v1.json.  We therefore join the crate
    // directory with "../contracts".

    let mut out_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    out_path.pop(); // remove "frontend"
    out_path.push("contracts");
    fs::create_dir_all(&out_path).expect("create contracts dir");
    out_path.push("frontend-v1.json");

    // ------------------------------------------------------------------
    // 3. Write (pretty-printed) JSON --------------------------------------
    // ------------------------------------------------------------------
    fs::write(&out_path, serde_json::to_string_pretty(&pact_json).unwrap())
        .unwrap_or_else(|e| panic!("cannot write pact file {:?}: {e}", out_path));

    println!("✅ Pact contract written to {}", out_path.display());
}
