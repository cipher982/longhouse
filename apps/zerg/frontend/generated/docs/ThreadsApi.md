# \ThreadsApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**create_thread_api_threads_post**](ThreadsApi.md#create_thread_api_threads_post) | **POST** /api/threads | Create Thread
[**create_thread_api_threads_post_0**](ThreadsApi.md#create_thread_api_threads_post_0) | **POST** /api/threads/ | Create Thread
[**create_thread_message_api_threads_thread_id_messages_post**](ThreadsApi.md#create_thread_message_api_threads_thread_id_messages_post) | **POST** /api/threads/{thread_id}/messages | Create Thread Message
[**delete_thread_api_threads_thread_id_delete**](ThreadsApi.md#delete_thread_api_threads_thread_id_delete) | **DELETE** /api/threads/{thread_id} | Delete Thread
[**read_thread_api_threads_thread_id_get**](ThreadsApi.md#read_thread_api_threads_thread_id_get) | **GET** /api/threads/{thread_id} | Read Thread
[**read_thread_messages_api_threads_thread_id_messages_get**](ThreadsApi.md#read_thread_messages_api_threads_thread_id_messages_get) | **GET** /api/threads/{thread_id}/messages | Read Thread Messages
[**read_threads_api_threads_get**](ThreadsApi.md#read_threads_api_threads_get) | **GET** /api/threads | Read Threads
[**read_threads_api_threads_get_0**](ThreadsApi.md#read_threads_api_threads_get_0) | **GET** /api/threads/ | Read Threads
[**run_thread_api_threads_thread_id_run_post**](ThreadsApi.md#run_thread_api_threads_thread_id_run_post) | **POST** /api/threads/{thread_id}/run | Run Thread
[**update_thread_api_threads_thread_id_put**](ThreadsApi.md#update_thread_api_threads_thread_id_put) | **PUT** /api/threads/{thread_id} | Update Thread



## create_thread_api_threads_post

> models::Thread create_thread_api_threads_post(thread_create, session_factory)
Create Thread

Create a new thread

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**thread_create** | [**ThreadCreate**](ThreadCreate.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::Thread**](Thread.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## create_thread_api_threads_post_0

> models::Thread create_thread_api_threads_post_0(thread_create, session_factory)
Create Thread

Create a new thread

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**thread_create** | [**ThreadCreate**](ThreadCreate.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::Thread**](Thread.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## create_thread_message_api_threads_thread_id_messages_post

> models::ThreadMessageResponse create_thread_message_api_threads_thread_id_messages_post(thread_id, thread_message_create, session_factory)
Create Thread Message

Create a new message in a thread

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**thread_id** | **i32** |  | [required] |
**thread_message_create** | [**ThreadMessageCreate**](ThreadMessageCreate.md) |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::ThreadMessageResponse**](ThreadMessageResponse.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## delete_thread_api_threads_thread_id_delete

> delete_thread_api_threads_thread_id_delete(thread_id, session_factory)
Delete Thread

Delete a thread

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**thread_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

 (empty response body)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## read_thread_api_threads_thread_id_get

> models::Thread read_thread_api_threads_thread_id_get(thread_id, session_factory)
Read Thread

Get a specific thread by ID

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**thread_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::Thread**](Thread.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## read_thread_messages_api_threads_thread_id_messages_get

> Vec<models::ThreadMessageResponse> read_thread_messages_api_threads_thread_id_messages_get(thread_id, skip, limit, session_factory)
Read Thread Messages

Get all messages for a thread

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**thread_id** | **i32** |  | [required] |
**skip** | Option<**i32**> |  |  |[default to 0]
**limit** | Option<**i32**> |  |  |[default to 100]
**session_factory** | Option<**String**> |  |  |

### Return type

[**Vec<models::ThreadMessageResponse>**](ThreadMessageResponse.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## read_threads_api_threads_get

> Vec<models::Thread> read_threads_api_threads_get(agent_id, skip, limit, session_factory)
Read Threads

Get all threads, optionally filtered by agent_id

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | Option<**i32**> |  |  |
**skip** | Option<**i32**> |  |  |[default to 0]
**limit** | Option<**i32**> |  |  |[default to 100]
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**Vec<models::Thread>**](Thread.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## read_threads_api_threads_get_0

> Vec<models::Thread> read_threads_api_threads_get_0(agent_id, skip, limit, session_factory)
Read Threads

Get all threads, optionally filtered by agent_id

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | Option<**i32**> |  |  |
**skip** | Option<**i32**> |  |  |[default to 0]
**limit** | Option<**i32**> |  |  |[default to 100]
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**Vec<models::Thread>**](Thread.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## run_thread_api_threads_thread_id_run_post

> serde_json::Value run_thread_api_threads_thread_id_run_post(thread_id, session_factory)
Run Thread

Process any unprocessed messages in the thread and stream back the result.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**thread_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## update_thread_api_threads_thread_id_put

> models::Thread update_thread_api_threads_thread_id_put(thread_id, thread_update, session_factory)
Update Thread

Update a thread

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**thread_id** | **i32** |  | [required] |
**thread_update** | [**ThreadUpdate**](ThreadUpdate.md) |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::Thread**](Thread.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

