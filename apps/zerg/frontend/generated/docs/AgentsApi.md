# \AgentsApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**create_agent_api_agents_post**](AgentsApi.md#create_agent_api_agents_post) | **POST** /api/agents | Create Agent
[**create_agent_api_agents_post_0**](AgentsApi.md#create_agent_api_agents_post_0) | **POST** /api/agents/ | Create Agent
[**create_agent_message_api_agents_agent_id_messages_post**](AgentsApi.md#create_agent_message_api_agents_agent_id_messages_post) | **POST** /api/agents/{agent_id}/messages | Create Agent Message
[**delete_agent_api_agents_agent_id_delete**](AgentsApi.md#delete_agent_api_agents_agent_id_delete) | **DELETE** /api/agents/{agent_id} | Delete Agent
[**read_agent_api_agents_agent_id_get**](AgentsApi.md#read_agent_api_agents_agent_id_get) | **GET** /api/agents/{agent_id} | Read Agent
[**read_agent_details_api_agents_agent_id_details_get**](AgentsApi.md#read_agent_details_api_agents_agent_id_details_get) | **GET** /api/agents/{agent_id}/details | Read Agent Details
[**read_agent_messages_api_agents_agent_id_messages_get**](AgentsApi.md#read_agent_messages_api_agents_agent_id_messages_get) | **GET** /api/agents/{agent_id}/messages | Read Agent Messages
[**read_agents_api_agents_get**](AgentsApi.md#read_agents_api_agents_get) | **GET** /api/agents | Read Agents
[**read_agents_api_agents_get_0**](AgentsApi.md#read_agents_api_agents_get_0) | **GET** /api/agents/ | Read Agents
[**run_agent_task_api_agents_agent_id_task_post**](AgentsApi.md#run_agent_task_api_agents_agent_id_task_post) | **POST** /api/agents/{agent_id}/task | Run Agent Task
[**update_agent_api_agents_agent_id_put**](AgentsApi.md#update_agent_api_agents_agent_id_put) | **PUT** /api/agents/{agent_id} | Update Agent



## create_agent_api_agents_post

> models::Agent create_agent_api_agents_post(agent_create, session_factory)
Create Agent

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_create** | [**AgentCreate**](AgentCreate.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::Agent**](Agent.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## create_agent_api_agents_post_0

> models::Agent create_agent_api_agents_post_0(agent_create, session_factory)
Create Agent

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_create** | [**AgentCreate**](AgentCreate.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::Agent**](Agent.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## create_agent_message_api_agents_agent_id_messages_post

> models::MessageResponse create_agent_message_api_agents_agent_id_messages_post(agent_id, message_create, session_factory)
Create Agent Message

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**message_create** | [**MessageCreate**](MessageCreate.md) |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::MessageResponse**](MessageResponse.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## delete_agent_api_agents_agent_id_delete

> delete_agent_api_agents_agent_id_delete(agent_id, session_factory)
Delete Agent

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

 (empty response body)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## read_agent_api_agents_agent_id_get

> models::Agent read_agent_api_agents_agent_id_get(agent_id, session_factory)
Read Agent

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::Agent**](Agent.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## read_agent_details_api_agents_agent_id_details_get

> models::AgentDetails read_agent_details_api_agents_agent_id_details_get(agent_id, include, session_factory)
Read Agent Details

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**include** | Option<**String**> |  |  |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::AgentDetails**](AgentDetails.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## read_agent_messages_api_agents_agent_id_messages_get

> Vec<models::MessageResponse> read_agent_messages_api_agents_agent_id_messages_get(agent_id, skip, limit, session_factory)
Read Agent Messages

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**skip** | Option<**i32**> |  |  |[default to 0]
**limit** | Option<**i32**> |  |  |[default to 100]
**session_factory** | Option<**String**> |  |  |

### Return type

[**Vec<models::MessageResponse>**](MessageResponse.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## read_agents_api_agents_get

> Vec<models::Agent> read_agents_api_agents_get(scope, skip, limit, session_factory)
Read Agents

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**scope** | Option<**String**> |  |  |[default to my]
**skip** | Option<**i32**> |  |  |[default to 0]
**limit** | Option<**i32**> |  |  |[default to 100]
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**Vec<models::Agent>**](Agent.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## read_agents_api_agents_get_0

> Vec<models::Agent> read_agents_api_agents_get_0(scope, skip, limit, session_factory)
Read Agents

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**scope** | Option<**String**> |  |  |[default to my]
**skip** | Option<**i32**> |  |  |[default to 0]
**limit** | Option<**i32**> |  |  |[default to 100]
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**Vec<models::Agent>**](Agent.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## run_agent_task_api_agents_agent_id_task_post

> serde_json::Value run_agent_task_api_agents_agent_id_task_post(agent_id, session_factory)
Run Agent Task

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


## update_agent_api_agents_agent_id_put

> models::Agent update_agent_api_agents_agent_id_put(agent_id, agent_update, session_factory)
Update Agent

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**agent_update** | [**AgentUpdate**](AgentUpdate.md) |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::Agent**](Agent.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

