/**
 * Manual test script to verify canvas data persistence
 * This can be run in the browser console to test the flow
 */

console.log('🔧 Manual Canvas Data Persistence Test');

// Step 1: Simulate creating agents
async function createTestAgent() {
    const response = await fetch('http://localhost:8001/api/agents', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${localStorage.getItem('jwt')}`
        },
        body: JSON.stringify({
            name: 'Test Agent',
            system_instructions: 'You are a test agent',
            task_instructions: 'Perform test tasks'
        })
    });
    
    if (response.ok) {
        const agent = await response.json();
        console.log('✅ Created test agent:', agent);
        return agent;
    } else {
        console.error('❌ Failed to create agent:', response.statusText);
        return null;
    }
}

// Step 2: Test workflow canvas data update
async function testCanvasDataUpdate() {
    const testCanvasData = {
        nodes: [
            {
                node_id: 'test-node-1',
                agent_id: 1,
                x: 100,
                y: 100,
                width: 200,
                height: 80,
                color: '#2ecc71',
                text: 'Test Agent',
                node_type: 'AgentIdentity',
                parent_id: null,
                is_selected: false,
                is_dragging: false,
                exec_status: null
            }
        ],
        edges: [
            {
                id: 'test-edge-1',
                from_node_id: 'test-node-1',
                to_node_id: 'test-node-2',
                label: null
            }
        ]
    };
    
    const response = await fetch('http://localhost:8001/api/workflows/current/canvas-data', {
        method: 'PATCH',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${localStorage.getItem('jwt')}`
        },
        body: JSON.stringify({
            canvas_data: testCanvasData
        })
    });
    
    if (response.ok) {
        const workflow = await response.json();
        console.log('✅ Canvas data updated:', workflow);
        return workflow;
    } else {
        console.error('❌ Failed to update canvas data:', response.statusText, await response.text());
        return null;
    }
}

// Step 3: Verify the data was saved
async function verifyCanvasData() {
    const response = await fetch('http://localhost:8001/api/workflows/current', {
        headers: {
            'Authorization': `Bearer ${localStorage.getItem('jwt')}`
        }
    });
    
    if (response.ok) {
        const workflow = await response.json();
        console.log('✅ Current workflow:', workflow);
        console.log('📋 Canvas data nodes:', workflow.canvas_data?.nodes?.length || 0);
        console.log('📋 Canvas data edges:', workflow.canvas_data?.edges?.length || 0);
        return workflow;
    } else {
        console.error('❌ Failed to get current workflow:', response.statusText);
        return null;
    }
}

// Run the test sequence
async function runTest() {
    console.log('🚀 Starting canvas data persistence test...');
    
    // Check if user is logged in
    const jwt = localStorage.getItem('jwt');
    if (!jwt) {
        console.error('❌ Not logged in - please log in first');
        return;
    }
    
    try {
        // Test canvas data update
        console.log('📝 Testing canvas data update...');
        const updatedWorkflow = await testCanvasDataUpdate();
        
        if (updatedWorkflow) {
            // Verify the data was saved
            console.log('🔍 Verifying saved data...');
            await new Promise(resolve => setTimeout(resolve, 1000)); // Wait a second
            const currentWorkflow = await verifyCanvasData();
            
            if (currentWorkflow && currentWorkflow.canvas_data) {
                const nodeCount = currentWorkflow.canvas_data.nodes?.length || 0;
                const edgeCount = currentWorkflow.canvas_data.edges?.length || 0;
                
                if (nodeCount > 0 && edgeCount > 0) {
                    console.log('🎉 SUCCESS: Canvas data persistence is working!');
                    console.log(`   - Nodes saved: ${nodeCount}`);
                    console.log(`   - Edges saved: ${edgeCount}`);
                } else {
                    console.log('⚠️ WARNING: Canvas data appears empty');
                }
            } else {
                console.log('❌ FAILED: No canvas data found in workflow');
            }
        }
    } catch (error) {
        console.error('❌ Test failed with error:', error);
    }
}

// Export for manual execution
window.testCanvasDataPersistence = runTest;
console.log('💡 Run window.testCanvasDataPersistence() to start the test');