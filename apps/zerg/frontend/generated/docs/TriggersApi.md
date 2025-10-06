# \TriggersApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**create_trigger_api_triggers_post**](TriggersApi.md#create_trigger_api_triggers_post) | **POST** /api/triggers/ | Create Trigger
[**delete_trigger_api_triggers_trigger_id_delete**](TriggersApi.md#delete_trigger_api_triggers_trigger_id_delete) | **DELETE** /api/triggers/{trigger_id} | Delete Trigger
[**fire_trigger_event_api_triggers_trigger_id_events_post**](TriggersApi.md#fire_trigger_event_api_triggers_trigger_id_events_post) | **POST** /api/triggers/{trigger_id}/events | Fire Trigger Event
[**list_triggers_api_triggers_get**](TriggersApi.md#list_triggers_api_triggers_get) | **GET** /api/triggers/ | List Triggers



## create_trigger_api_triggers_post

> models::Trigger create_trigger_api_triggers_post(trigger_create, session_factory)
Create Trigger

Create a new trigger for an agent.  If the trigger is of type *email* and the provider is **gmail** we kick off an asynchronous helper that ensures a Gmail *watch* is registered.  The call is awaited so tests (which run inside the same event-loop) can verify the side-effects synchronously without sprinkling ``asyncio.sleep`` hacks.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**trigger_create** | [**TriggerCreate**](TriggerCreate.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::Trigger**](Trigger.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## delete_trigger_api_triggers_trigger_id_delete

> delete_trigger_api_triggers_trigger_id_delete(trigger_id, session_factory)
Delete Trigger

Delete a trigger.  Special handling for *email* provider **gmail**:  • Attempts to call Gmail *stop* endpoint so push notifications are   turned off immediately on user’s mailbox.  The call is best effort –   network/auth failures are logged but do not abort the deletion.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**trigger_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

 (empty response body)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## fire_trigger_event_api_triggers_trigger_id_events_post

> serde_json::Value fire_trigger_event_api_triggers_trigger_id_events_post(trigger_id, x_zerg_timestamp, x_zerg_signature, session_factory, body)
Fire Trigger Event

Webhook endpoint that fires a trigger event.  Security: the caller must sign the request body using HMAC-SHA256.  Signature string to hash:     \"{timestamp}.{raw_body}\"  where *timestamp* is the same value sent in `X-Zerg-Timestamp` header and *raw_body* is the exact JSON body (no whitespace changes).  The hex-encoded digest is provided via `X-Zerg-Signature` header.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**trigger_id** | **i32** |  | [required] |
**x_zerg_timestamp** | **String** |  | [required] |
**x_zerg_signature** | **String** |  | [required] |
**session_factory** | Option<**String**> |  |  |
**body** | Option<**serde_json::Value**> |  |  |

### Return type

[**serde_json::Value**](serde_json::Value.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## list_triggers_api_triggers_get

> Vec<models::Trigger> list_triggers_api_triggers_get(agent_id, session_factory)
List Triggers

List all triggers, optionally filtered by agent_id.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**agent_id** | Option<**i32**> | Filter triggers by agent ID |  |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**Vec<models::Trigger>**](Trigger.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

