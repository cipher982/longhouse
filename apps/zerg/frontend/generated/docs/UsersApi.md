# \UsersApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**read_current_user_api_users_me_get**](UsersApi.md#read_current_user_api_users_me_get) | **GET** /api/users/me | Read Current User
[**update_current_user_api_users_me_put**](UsersApi.md#update_current_user_api_users_me_put) | **PUT** /api/users/me | Update Current User
[**upload_current_user_avatar_api_users_me_avatar_post**](UsersApi.md#upload_current_user_avatar_api_users_me_avatar_post) | **POST** /api/users/me/avatar | Upload Current User Avatar



## read_current_user_api_users_me_get

> models::UserOut read_current_user_api_users_me_get(session_factory)
Read Current User

Return the authenticated user's profile.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::UserOut**](UserOut.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## update_current_user_api_users_me_put

> models::UserOut update_current_user_api_users_me_put(user_update, session_factory)
Update Current User

Patch the authenticated user's profile (display name, avatar, prefs).

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**user_update** | [**UserUpdate**](UserUpdate.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::UserOut**](UserOut.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## upload_current_user_avatar_api_users_me_avatar_post

> models::UserOut upload_current_user_avatar_api_users_me_avatar_post(file, session_factory)
Upload Current User Avatar

Handle *multipart/form-data* avatar upload for the authenticated user.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**file** | **std::path::PathBuf** | Avatar image file (PNG/JPEG/WebP ≤2 MB) | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::UserOut**](UserOut.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: multipart/form-data
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

