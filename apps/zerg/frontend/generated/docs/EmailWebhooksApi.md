# \EmailWebhooksApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**gmail_webhook_api_email_webhook_google_post**](EmailWebhooksApi.md#gmail_webhook_api_email_webhook_google_post) | **POST** /api/email/webhook/google | Gmail Webhook



## gmail_webhook_api_email_webhook_google_post

> serde_json::Value gmail_webhook_api_email_webhook_google_post(x_goog_channel_token, session_factory, x_goog_resource_id, x_goog_message_number, authorization, body)
Gmail Webhook

Handle Gmail *watch* callbacks.  The implementation is an **MVP**: every callback simply triggers all *gmail* email-type triggers.  Later versions will match the *resourceId* to a specific user and run the Gmail *history* API to fetch only the new messages.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**x_goog_channel_token** | **String** |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |
**x_goog_resource_id** | Option<**String**> |  |  |
**x_goog_message_number** | Option<**String**> |  |  |
**authorization** | Option<**String**> |  |  |
**body** | Option<**serde_json::Value**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

