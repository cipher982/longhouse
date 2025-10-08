# \WorkflowsApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**create_workflow_api_workflows_post**](WorkflowsApi.md#create_workflow_api_workflows_post) | **POST** /api/workflows/ | Create Workflow
[**delete_workflow_api_workflows_workflow_id_delete**](WorkflowsApi.md#delete_workflow_api_workflows_workflow_id_delete) | **DELETE** /api/workflows/{workflow_id} | Delete Workflow
[**get_current_workflow_api_workflows_current_get**](WorkflowsApi.md#get_current_workflow_api_workflows_current_get) | **GET** /api/workflows/current | Get Current Workflow
[**get_workflow_layout_api_workflows_workflow_id_layout_get**](WorkflowsApi.md#get_workflow_layout_api_workflows_workflow_id_layout_get) | **GET** /api/workflows/{workflow_id}/layout | Get Workflow Layout
[**put_workflow_layout_api_workflows_workflow_id_layout_put**](WorkflowsApi.md#put_workflow_layout_api_workflows_workflow_id_layout_put) | **PUT** /api/workflows/{workflow_id}/layout | Put Workflow Layout
[**read_workflows_api_workflows_get**](WorkflowsApi.md#read_workflows_api_workflows_get) | **GET** /api/workflows/ | Read Workflows
[**rename_workflow_api_workflows_workflow_id_patch**](WorkflowsApi.md#rename_workflow_api_workflows_workflow_id_patch) | **PATCH** /api/workflows/{workflow_id} | Rename Workflow
[**update_current_workflow_canvas_api_workflows_current_canvas_patch**](WorkflowsApi.md#update_current_workflow_canvas_api_workflows_current_canvas_patch) | **PATCH** /api/workflows/current/canvas | Update Current Workflow Canvas
[**validate_workflow_api_workflows_validate_post**](WorkflowsApi.md#validate_workflow_api_workflows_validate_post) | **POST** /api/workflows/validate | Validate Workflow



## create_workflow_api_workflows_post

> models::Workflow create_workflow_api_workflows_post(workflow_create, session_factory)
Create Workflow

Create new workflow. Rate limited to 100 workflows per minute per user.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_create** | [**WorkflowCreate**](WorkflowCreate.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::Workflow**](Workflow.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## delete_workflow_api_workflows_workflow_id_delete

> delete_workflow_api_workflows_workflow_id_delete(workflow_id, session_factory)
Delete Workflow

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

 (empty response body)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## get_current_workflow_api_workflows_current_get

> models::Workflow get_current_workflow_api_workflows_current_get(session_factory)
Get Current Workflow

Get the user's current working workflow. Creates a default workflow if none exists.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::Workflow**](Workflow.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## get_workflow_layout_api_workflows_workflow_id_layout_get

> serde_json::Value get_workflow_layout_api_workflows_workflow_id_layout_get(workflow_id, session_factory)
Get Workflow Layout

Return the stored canvas layout for the workflow or **204** when empty.

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


## put_workflow_layout_api_workflows_workflow_id_layout_put

> put_workflow_layout_api_workflows_workflow_id_layout_put(workflow_id, layout_update, session_factory)
Put Workflow Layout

Persist the canvas layout for **workflow_id** owned by *current_user*.  The endpoint completely replaces the stored layout – callers should send the full `nodes` + `viewport` payload (same schema as `/api/graph/layout`).

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_id** | **i32** |  | [required] |
**layout_update** | [**LayoutUpdate**](LayoutUpdate.md) |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

 (empty response body)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## read_workflows_api_workflows_get

> Vec<models::Workflow> read_workflows_api_workflows_get(skip, limit, session_factory)
Read Workflows

Return all workflows owned by current user.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**skip** | Option<**i32**> |  |  |[default to 0]
**limit** | Option<**i32**> |  |  |[default to 100]
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**Vec<models::Workflow>**](Workflow.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## rename_workflow_api_workflows_workflow_id_patch

> models::Workflow rename_workflow_api_workflows_workflow_id_patch(workflow_id, workflow_update, session_factory)
Rename Workflow

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_id** | **i32** |  | [required] |
**workflow_update** | [**WorkflowUpdate**](WorkflowUpdate.md) |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::Workflow**](Workflow.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## update_current_workflow_canvas_api_workflows_current_canvas_patch

> models::Workflow update_current_workflow_canvas_api_workflows_current_canvas_patch(canvas_update, session_factory)
Update Current Workflow Canvas

Update the canvas for the user's current workflow. Creates a default workflow if none exists.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**canvas_update** | [**CanvasUpdate**](CanvasUpdate.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::Workflow**](Workflow.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## validate_workflow_api_workflows_validate_post

> models::ValidationResponse validate_workflow_api_workflows_validate_post(canvas_update, session_factory)
Validate Workflow

Validate workflow canvas data without saving.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**canvas_update** | [**CanvasUpdate**](CanvasUpdate.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::ValidationResponse**](ValidationResponse.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

