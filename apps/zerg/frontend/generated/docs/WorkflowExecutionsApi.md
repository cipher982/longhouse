# \WorkflowExecutionsApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**cancel_execution_api_workflow_executions_execution_id_cancel_patch**](WorkflowExecutionsApi.md#cancel_execution_api_workflow_executions_execution_id_cancel_patch) | **PATCH** /api/workflow-executions/{execution_id}/cancel | Cancel Execution
[**export_execution_data_api_workflow_executions_execution_id_export_get**](WorkflowExecutionsApi.md#export_execution_data_api_workflow_executions_execution_id_export_get) | **GET** /api/workflow-executions/{execution_id}/export | Export Execution Data
[**get_execution_history_api_workflow_executions_history_workflow_id_get**](WorkflowExecutionsApi.md#get_execution_history_api_workflow_executions_history_workflow_id_get) | **GET** /api/workflow-executions/history/{workflow_id} | Get Execution History
[**get_execution_logs_api_workflow_executions_execution_id_logs_get**](WorkflowExecutionsApi.md#get_execution_logs_api_workflow_executions_execution_id_logs_get) | **GET** /api/workflow-executions/{execution_id}/logs | Get Execution Logs
[**get_execution_status_api_workflow_executions_execution_id_status_get**](WorkflowExecutionsApi.md#get_execution_status_api_workflow_executions_execution_id_status_get) | **GET** /api/workflow-executions/{execution_id}/status | Get Execution Status
[**get_workflow_schedule_api_workflow_executions_workflow_id_schedule_get**](WorkflowExecutionsApi.md#get_workflow_schedule_api_workflow_executions_workflow_id_schedule_get) | **GET** /api/workflow-executions/{workflow_id}/schedule | Get Workflow Schedule
[**list_scheduled_workflows_api_workflow_executions_scheduled_get**](WorkflowExecutionsApi.md#list_scheduled_workflows_api_workflow_executions_scheduled_get) | **GET** /api/workflow-executions/scheduled | List Scheduled Workflows
[**reserve_workflow_execution_api_workflow_executions_workflow_id_reserve_post**](WorkflowExecutionsApi.md#reserve_workflow_execution_api_workflow_executions_workflow_id_reserve_post) | **POST** /api/workflow-executions/{workflow_id}/reserve | Reserve Workflow Execution
[**schedule_workflow_api_workflow_executions_workflow_id_schedule_post**](WorkflowExecutionsApi.md#schedule_workflow_api_workflow_executions_workflow_id_schedule_post) | **POST** /api/workflow-executions/{workflow_id}/schedule | Schedule Workflow
[**start_reserved_execution_api_workflow_executions_executions_execution_id_start_post**](WorkflowExecutionsApi.md#start_reserved_execution_api_workflow_executions_executions_execution_id_start_post) | **POST** /api/workflow-executions/executions/{execution_id}/start | Start Reserved Execution
[**start_workflow_execution_api_workflow_executions_workflow_id_start_post**](WorkflowExecutionsApi.md#start_workflow_execution_api_workflow_executions_workflow_id_start_post) | **POST** /api/workflow-executions/{workflow_id}/start | Start Workflow Execution
[**unschedule_workflow_api_workflow_executions_workflow_id_schedule_delete**](WorkflowExecutionsApi.md#unschedule_workflow_api_workflow_executions_workflow_id_schedule_delete) | **DELETE** /api/workflow-executions/{workflow_id}/schedule | Unschedule Workflow



## cancel_execution_api_workflow_executions_execution_id_cancel_patch

> cancel_execution_api_workflow_executions_execution_id_cancel_patch(execution_id, cancel_payload, session_factory)
Cancel Execution

Mark a running workflow execution as *cancelled*.  The engine cooperatively checks the updated status before starting each new node and exits early. If the execution already finished the endpoint returns 409.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**execution_id** | **i32** |  | [required] |
**cancel_payload** | [**CancelPayload**](CancelPayload.md) |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

 (empty response body)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## export_execution_data_api_workflow_executions_execution_id_export_get

> serde_json::Value export_execution_data_api_workflow_executions_execution_id_export_get(execution_id, session_factory)
Export Execution Data

Export the data of a workflow execution.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**execution_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## get_execution_history_api_workflow_executions_history_workflow_id_get

> serde_json::Value get_execution_history_api_workflow_executions_history_workflow_id_get(workflow_id, session_factory)
Get Execution History

Get the execution history of a workflow.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## get_execution_logs_api_workflow_executions_execution_id_logs_get

> serde_json::Value get_execution_logs_api_workflow_executions_execution_id_logs_get(execution_id, session_factory)
Get Execution Logs

Get the logs of a workflow execution.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**execution_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## get_execution_status_api_workflow_executions_execution_id_status_get

> serde_json::Value get_execution_status_api_workflow_executions_execution_id_status_get(execution_id, session_factory)
Get Execution Status

Get the status of a workflow execution.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**execution_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## get_workflow_schedule_api_workflow_executions_workflow_id_schedule_get

> serde_json::Value get_workflow_schedule_api_workflow_executions_workflow_id_schedule_get(workflow_id, session_factory)
Get Workflow Schedule

Get the current schedule for a workflow.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## list_scheduled_workflows_api_workflow_executions_scheduled_get

> serde_json::Value list_scheduled_workflows_api_workflow_executions_scheduled_get(session_factory)
List Scheduled Workflows

List all scheduled workflows for the current user.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## reserve_workflow_execution_api_workflow_executions_workflow_id_reserve_post

> serde_json::Value reserve_workflow_execution_api_workflow_executions_workflow_id_reserve_post(workflow_id, session_factory)
Reserve Workflow Execution

Reserve an execution ID for a workflow without starting execution. This allows the frontend to subscribe to WebSocket messages before execution starts.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## schedule_workflow_api_workflow_executions_workflow_id_schedule_post

> serde_json::Value schedule_workflow_api_workflow_executions_workflow_id_schedule_post(workflow_id, schedule_workflow_payload, session_factory)
Schedule Workflow

Schedule a workflow to run on a cron schedule.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_id** | **i32** |  | [required] |
**schedule_workflow_payload** | [**ScheduleWorkflowPayload**](ScheduleWorkflowPayload.md) |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## start_reserved_execution_api_workflow_executions_executions_execution_id_start_post

> serde_json::Value start_reserved_execution_api_workflow_executions_executions_execution_id_start_post(execution_id, session_factory)
Start Reserved Execution

Start a previously reserved execution.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**execution_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## start_workflow_execution_api_workflow_executions_workflow_id_start_post

> serde_json::Value start_workflow_execution_api_workflow_executions_workflow_id_start_post(workflow_id, session_factory)
Start Workflow Execution

Start a new execution of a workflow using LangGraph engine. Uses the original synchronous approach.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## unschedule_workflow_api_workflow_executions_workflow_id_schedule_delete

> serde_json::Value unschedule_workflow_api_workflow_executions_workflow_id_schedule_delete(workflow_id, session_factory)
Unschedule Workflow

Remove the schedule for a workflow.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

