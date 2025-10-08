# \RunsApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**get_run_api_runs_run_id_get**](RunsApi.md#get_run_api_runs_run_id_get) | **GET** /api/runs/{run_id} | Get Run
[**list_agent_runs_api_agents_agent_id_runs_get**](RunsApi.md#list_agent_runs_api_agents_agent_id_runs_get) | **GET** /api/agents/{agent_id}/runs | List Agent Runs



## get_run_api_runs_run_id_get

> models::AgentRunOut get_run_api_runs_run_id_get(run_id, session_factory)
Get Run

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**run_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::AgentRunOut**](AgentRunOut.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## list_agent_runs_api_agents_agent_id_runs_get

> Vec<models::AgentRunOut> list_agent_runs_api_agents_agent_id_runs_get(agent_id, limit, session_factory)
List Agent Runs

Return latest *limit* runs for the given agent (descending).

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | **i32** |  | [required] |
**limit** | Option<**i32**> |  |  |[default to 20]
**session_factory** | Option<**String**> |  |  |

### Return type

[**Vec<models::AgentRunOut>**](AgentRunOut.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

