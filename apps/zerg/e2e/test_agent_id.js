const fetch = require('node-fetch');

async function testAgentIdFix() {
    try {
        console.log('🔧 Testing agent_id fix...');
        
        const response = await fetch('http://localhost:8001/api/workflows/current');
        if (response.ok) {
            const workflow = await response.json();
            console.log('📋 Current workflow canvas_data:');
            console.log(JSON.stringify(workflow.canvas_data, null, 2));
            
            if (workflow.canvas_data && workflow.canvas_data.nodes) {
                console.log('\n🔍 Node details:');
                workflow.canvas_data.nodes.forEach((node, index) => {
                    console.log(`Node ${index}:`);
                    console.log(`  - node_id: ${node.node_id}`);
                    console.log(`  - agent_id: ${node.agent_id}`);
                    console.log(`  - text: ${node.text}`);
                    console.log(`  - node_type: ${node.node_type}`);
                    
                    if (node.agent_id !== null) {
                        console.log(`  ✅ Agent ID is properly set!`);
                    } else {
                        console.log(`  ❌ Agent ID is still null`);
                    }
                });
                
                // Test workflow execution if we have nodes with proper agent_ids
                const hasValidAgentIds = workflow.canvas_data.nodes.some(node => node.agent_id !== null);
                if (hasValidAgentIds) {
                    console.log('\n🚀 Testing workflow execution with proper agent IDs...');
                    const executionResponse = await fetch('http://localhost:8001/api/workflow-executions/1/start', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({})
                    });
                    
                    console.log('📋 Execution response status:', executionResponse.status);
                    
                    if (executionResponse.ok) {
                        console.log('🎉 SUCCESS: Workflow execution with real agent IDs worked!');
                        const result = await executionResponse.json();
                        console.log('📋 Execution result:', result);
                    } else {
                        console.log('❌ Workflow execution still failed:', executionResponse.status);
                        const errorText = await executionResponse.text();
                        console.log('Error:', errorText);
                    }
                } else {
                    console.log('\n⚠️ No nodes with valid agent_ids found, skipping execution test');
                }
            }
        } else {
            console.log('❌ Failed to get workflow:', response.status);
        }
    } catch (error) {
        console.error('❌ Test failed:', error);
    }
}

testAgentIdFix();