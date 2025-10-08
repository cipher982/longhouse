# \CanvasApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**get_layout_api_graph_layout_get**](CanvasApi.md#get_layout_api_graph_layout_get) | **GET** /api/graph/layout | Get Layout
[**patch_layout_api_graph_layout_patch**](CanvasApi.md#patch_layout_api_graph_layout_patch) | **PATCH** /api/graph/layout | Patch Layout



## get_layout_api_graph_layout_get

> serde_json::Value get_layout_api_graph_layout_get(workflow_id, session_factory)
Get Layout

Return the stored layout for the authenticated user (if any).

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_id** | Option<**i32**> |  |  |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## patch_layout_api_graph_layout_patch

> patch_layout_api_graph_layout_patch(layout_update, workflow_id, session_factory)
Patch Layout

Upsert the authenticated user's canvas layout.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**layout_update** | [**LayoutUpdate**](LayoutUpdate.md) |  | [required] |
**workflow_id** | Option<**i32**> |  |  |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

 (empty response body)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

