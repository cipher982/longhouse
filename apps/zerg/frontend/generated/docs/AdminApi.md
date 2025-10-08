# \AdminApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**reset_database_api_admin_reset_database_post**](AdminApi.md#reset_database_api_admin_reset_database_post) | **POST** /api/admin/reset-database | Reset Database



## reset_database_api_admin_reset_database_post

> serde_json::Value reset_database_api_admin_reset_database_post(session_factory)
Reset Database

Reset the database by dropping all tables and recreating them. For development only.

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

