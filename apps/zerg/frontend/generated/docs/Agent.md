# Agent

## Properties

Name | Type | Description | Notes
------------ | ------------- | ------------- | -------------
**name** | **String** |  | 
**system_instructions** | **String** |  | 
**task_instructions** | **String** |  | 
**model** | **String** |  | 
**schedule** | Option<**String**> |  | [optional]
**config** | Option<[**serde_json::Value**](.md)> |  | [optional]
**last_error** | Option<**String**> |  | [optional]
**allowed_tools** | Option<**Vec<String>**> |  | [optional]
**id** | **i32** |  | 
**owner_id** | **i32** |  | 
**owner** | Option<[**models::UserOut**](UserOut.md)> |  | [optional]
**status** | **String** |  | 
**created_at** | **String** |  | 
**updated_at** | **String** |  | 
**messages** | Option<[**Vec<models::AgentMessage>**](AgentMessage.md)> |  | [optional][default to []]
**next_run_at** | Option<**String**> |  | [optional]
**last_run_at** | Option<**String**> |  | [optional]

[[Back to Model list]](../README.md#documentation-for-models) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to README]](../README.md)


