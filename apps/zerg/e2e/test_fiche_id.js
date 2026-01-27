const fetch = require('node-fetch');

async function testFicheIdFix() {
    try {
        console.log('🔧 Testing fiche_id fix...');

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
                    console.log(`  - fiche_id: ${node.fiche_id}`);
                    console.log(`  - text: ${node.text}`);
                    console.log(`  - node_type: ${node.node_type}`);

                    if (node.fiche_id !== null) {
                        console.log(`  ✅ Fiche ID is properly set!`);
                    } else {
                        console.log(`  ❌ Fiche ID is still null`);
                    }
                });

                // Test workflow execution if we have nodes with proper fiche_ids
                const hasValidFicheIds = workflow.canvas_data.nodes.some(node => node.fiche_id !== null);
                if (hasValidFicheIds) {
                    console.log('\n🚀 Testing workflow execution with proper fiche IDs...');
                    const executionResponse = await fetch('http://localhost:8001/api/workflow-executions/1/start', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({})
                    });

                    console.log('📋 Execution response status:', executionResponse.status);

                    if (executionResponse.ok) {
                        console.log('🎉 SUCCESS: Workflow execution with real fiche IDs worked!');
                        const result = await executionResponse.json();
                        console.log('📋 Execution result:', result);
                    } else {
                        console.log('❌ Workflow execution still failed:', executionResponse.status);
                        const errorText = await executionResponse.text();
                        console.log('Error:', errorText);
                    }
                } else {
                    console.log('\n⚠️ No nodes with valid fiche_ids found, skipping execution test');
                }
            }
        } else {
            console.log('❌ Failed to get workflow:', response.status);
        }
    } catch (error) {
        console.error('❌ Test failed:', error);
    }
}

testFicheIdFix();
