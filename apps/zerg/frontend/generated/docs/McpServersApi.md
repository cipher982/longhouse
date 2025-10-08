# \McpServersApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**add_mcp_server_api_agents_agent_id_mcp_servers_post**](McpServersApi.md#add_mcp_server_api_agents_agent_id_mcp_servers_post) | **POST** /api/agents/{agent_id}/mcp-servers/ | Add Mcp Server
[**get_available_tools_api_agents_agent_id_mcp_servers_available_tools_get**](McpServersApi.md#get_available_tools_api_agents_agent_id_mcp_servers_available_tools_get) | **GET** /api/agents/{agent_id}/mcp-servers/available-tools | Get Available Tools
[**list_mcp_servers_api_agents_agent_id_mcp_servers_get**](McpServersApi.md#list_mcp_servers_api_agents_agent_id_mcp_servers_get) | **GET** /api/agents/{agent_id}/mcp-servers/ | List Mcp Servers
[**remove_mcp_server_api_agents_agent_id_mcp_servers_server_name_delete**](McpServersApi.md#remove_mcp_server_api_agents_agent_id_mcp_servers_server_name_delete) | **DELETE** /api/agents/{agent_id}/mcp-servers/{server_name} | Remove Mcp Server
[**test_mcp_connection_api_agents_agent_id_mcp_servers_test_post**](McpServersApi.md#test_mcp_connection_api_agents_agent_id_mcp_servers_test_post) | **POST** /api/agents/{agent_id}/mcp-servers/test | Test Mcp Connection



## add_mcp_server_api_agents_agent_id_mcp_servers_post

> models::Agent add_mcp_server_api_agents_agent_id_mcp_servers_post(agent_id, mcp_server_add_request, session_factory)
Add Mcp Server

Add an MCP server to an agent.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**mcp_server_add_request** | [**McpServerAddRequest**](McpServerAddRequest.md) |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::Agent**](Agent.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## get_available_tools_api_agents_agent_id_mcp_servers_available_tools_get

> serde_json::Value get_available_tools_api_agents_agent_id_mcp_servers_available_tools_get(agent_id, session_factory)
Get Available Tools

Get all available tools for an agent (built-in + MCP).

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## list_mcp_servers_api_agents_agent_id_mcp_servers_get

> Vec<models::McpServerResponse> list_mcp_servers_api_agents_agent_id_mcp_servers_get(agent_id, session_factory)
List Mcp Servers

List all MCP servers configured for an agent.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**Vec<models::McpServerResponse>**](MCPServerResponse.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## remove_mcp_server_api_agents_agent_id_mcp_servers_server_name_delete

> remove_mcp_server_api_agents_agent_id_mcp_servers_server_name_delete(agent_id, server_name, session_factory)
Remove Mcp Server

Remove an MCP server from an agent.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**server_name** | **String** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

 (empty response body)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## test_mcp_connection_api_agents_agent_id_mcp_servers_test_post

> models::McpTestConnectionResponse test_mcp_connection_api_agents_agent_id_mcp_servers_test_post(agent_id, mcp_server_add_request, session_factory)
Test Mcp Connection

Test connection to an MCP server without saving it.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**mcp_server_add_request** | [**McpServerAddRequest**](McpServerAddRequest.md) |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::McpTestConnectionResponse**](MCPTestConnectionResponse.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

