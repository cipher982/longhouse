# \TemplatesApi

All URIs are relative to *http://localhost:8001*

Method | HTTP request | Description
------------- | ------------- | -------------
[**create_template_api_templates_post**](TemplatesApi.md#create_template_api_templates_post) | **POST** /api/templates/ | Create Template
[**deploy_template_api_templates_deploy_post**](TemplatesApi.md#deploy_template_api_templates_deploy_post) | **POST** /api/templates/deploy | Deploy Template
[**get_template_api_templates_template_id_get**](TemplatesApi.md#get_template_api_templates_template_id_get) | **GET** /api/templates/{template_id} | Get Template
[**list_categories_api_templates_categories_get**](TemplatesApi.md#list_categories_api_templates_categories_get) | **GET** /api/templates/categories | List Categories
[**list_templates_api_templates_get**](TemplatesApi.md#list_templates_api_templates_get) | **GET** /api/templates/ | List Templates



## create_template_api_templates_post

> models::WorkflowTemplate create_template_api_templates_post(workflow_template_create, session_factory)
Create Template

Create new workflow template.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**workflow_template_create** | [**WorkflowTemplateCreate**](WorkflowTemplateCreate.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::WorkflowTemplate**](WorkflowTemplate.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## deploy_template_api_templates_deploy_post

> models::Workflow deploy_template_api_templates_deploy_post(template_deploy_request, session_factory)
Deploy Template

Deploy a template as a new workflow.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**template_deploy_request** | [**TemplateDeployRequest**](TemplateDeployRequest.md) |  | [required] |
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**models::Workflow**](Workflow.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: application/json
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## get_template_api_templates_template_id_get

> models::WorkflowTemplate get_template_api_templates_template_id_get(template_id, session_factory)
Get Template

Get a specific template by ID.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**template_id** | **i32** |  | [required] |
**session_factory** | Option<**String**> |  |  |

### Return type

[**models::WorkflowTemplate**](WorkflowTemplate.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## list_categories_api_templates_categories_get

> Vec<String> list_categories_api_templates_categories_get(session_factory)
List Categories

Get all available template categories.

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

**Vec<String>**

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)


## list_templates_api_templates_get

> Vec<models::WorkflowTemplate> list_templates_api_templates_get(category, skip, limit, my_templates, session_factory)
List Templates

List workflow templates. By default shows public templates. Set my_templates=true to see your own templates (public and private).

### Parameters


Name | Type | Description  | Required | Notes
------------- | ------------- | ------------- | ------------- | -------------
**category** | Option<**String**> |  |  |
**skip** | Option<**i32**> |  |  |[default to 0]
**limit** | Option<**i32**> |  |  |[default to 100]
**my_templates** | Option<**bool**> |  |  |[default to false]
**session_factory** | Option<[**serde_json::Value**](.md)> |  |  |

### Return type

[**Vec<models::WorkflowTemplate>**](WorkflowTemplate.md)

### Authorization

No authorization required

### HTTP request headers

- **Content-Type**: Not defined
- **Accept**: application/json

[[Back to top]](#) [[Back to API list]](../README.md#documentation-for-api-endpoints) [[Back to Model list]](../README.md#documentation-for-models) [[Back to README]](../README.md)

