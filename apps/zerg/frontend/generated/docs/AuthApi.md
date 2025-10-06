# \AuthApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**connect_gmail_api_auth_google_gmail_post**](AuthApi.md#connect_gmail_api_auth_google_gmail_post) | **POST** /api/auth/google/gmail | Connect Gmail
[**google_sign_in_api_auth_google_post**](AuthApi.md#google_sign_in_api_auth_google_post) | **POST** /api/auth/google | Google Sign In



## connect_gmail_api_auth_google_gmail_post

> std::collections::HashMap<String, String> connect_gmail_api_auth_google_gmail_post(request_body, session_factory)
Connect Gmail

Store *offline* Gmail permissions for the **current** user.  Expected body: ``{ \"auth_code\": \"<code from OAuth consent window>\" }``.  The frontend must request the following when launching the consent screen::      scope=https://www.googleapis.com/auth/gmail.readonly     access_type=offline     prompt=consent  The *refresh token* returned by Google is stored on the user row.  The endpoint returns a simple JSON confirmation so the client knows the account is connected.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**request_body** | [**std::collections::HashMap<String, String>**](String.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

**std::collections::HashMap<String, String>**

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## google_sign_in_api_auth_google_post

> models::TokenOut google_sign_in_api_auth_google_post(request_body, session_factory)
Google Sign In

Exchange a Google ID token for a platform access token.  Expected JSON body: `{ \"id_token\": \"<JWT from Google>\" }`.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**request_body** | [**std::collections::HashMap<String, String>**](String.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::TokenOut**](TokenOut.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

