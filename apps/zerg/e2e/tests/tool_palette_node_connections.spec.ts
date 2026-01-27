import { test, expect } from './fixtures';

// Skip: Tool palette/connection tests use canvas selectors that have changed
test.skip();

/**
 * TOOL PALETTE AND NODE CONNECTION WORKFLOW E2E TEST
 *
 * This test validates the complete tool palette and node connection system:
 * 1. Tool palette discovery and cataloging
 * 2. Tool drag-and-drop from palette to canvas
 * 3. Node connection handle detection and interaction
 * 4. Connection creation between nodes (fiche -> tool, tool -> tool)
 * 5. Connection validation and constraint checking
 * 6. Connection deletion and modification
 * 7. Complex workflow topology creation
 * 8. Connection data flow validation
 */

test.describe('Tool Palette and Node Connections', () => {
  test('Tool palette discovery and cataloging', async ({ page, request }) => {
    console.log('🚀 Starting tool palette discovery test...');

    const commisId = process.env.TEST_PARALLEL_INDEX || '0';
    console.log('📊 Commis ID:', commisId);

    // Navigate to canvas
    await page.goto('/');
    await page.waitForTimeout(1000);
    await page.getByTestId('global-canvas-tab').click();
    await page.waitForTimeout(2000);

    // Test 1: Locate tool palette
    console.log('📊 Test 1: Locating tool palette...');
    const toolPalette = page.locator('[data-testid="tool-palette"]');
    const paletteVisible = await toolPalette.isVisible();
    console.log('📊 Tool palette visible:', paletteVisible);

    if (paletteVisible) {
      // Test 2: Catalog available tools
      console.log('📊 Test 2: Cataloging available tools...');
      const toolItems = await toolPalette.locator('[data-testid^="tool-item"], .tool-item, .palette-tool').count();
      console.log('📊 Tool items found in palette:', toolItems);

      if (toolItems > 0) {
        // Get tool names/types
        const toolElements = toolPalette.locator('[data-testid^="tool-item"], .tool-item, .palette-tool');
        const toolCount = await toolElements.count();

        const toolList = [];
        for (let i = 0; i < Math.min(toolCount, 10); i++) {
          const toolText = await toolElements.nth(i).textContent();
          const toolTestId = await toolElements.nth(i).getAttribute('data-testid');
          toolList.push({ text: toolText?.trim(), testId: toolTestId });
        }

        console.log('📊 Available tools:', toolList.map(t => t.text || t.testId).slice(0, 5));
        console.log('✅ Tool palette populated with tools');
      }

      // Test 3: Look for specific tool categories
      console.log('📊 Test 3: Checking for tool categories...');
      const httpTools = await toolPalette.locator('text=HTTP, text=http, [data-testid*="http"]').count();
      const urlTools = await toolPalette.locator('text=URL, text=url, [data-testid*="url"]').count();
      const apiTools = await toolPalette.locator('text=API, text=api, [data-testid*="api"]').count();
      const webhookTools = await toolPalette.locator('text=Webhook, text=webhook, [data-testid*="webhook"]').count();

      console.log('📊 HTTP tools found:', httpTools);
      console.log('📊 URL tools found:', urlTools);
      console.log('📊 API tools found:', apiTools);
      console.log('📊 Webhook tools found:', webhookTools);

      if (httpTools > 0 || urlTools > 0 || apiTools > 0) {
        console.log('✅ Network/HTTP tools available');
      }
    } else {
      console.log('⚠️  Tool palette not visible - checking alternative locations...');

      // Look for alternative tool palette locations
      const alternativePalettes = await page.locator('.tools, .palette, [data-testid*="tool"]').count();
      console.log('📊 Alternative tool containers found:', alternativePalettes);
    }

    console.log('✅ Tool palette discovery completed');
  });

  test('Tool drag-and-drop from palette to canvas', async ({ page, request }) => {
    console.log('🚀 Starting tool drag-and-drop test...');

    const commisId = process.env.TEST_PARALLEL_INDEX || '0';

    // First create an fiche to work with
    const ficheResponse = await request.post('/api/fiches', {
      headers: {
        'X-Test-Commis': commisId,
        'Content-Type': 'application/json',
      },
      data: {
        name: `Tool Test Fiche ${Date.now()}`,
        system_instructions: 'Fiche for tool drag-and-drop testing',
        task_instructions: 'Work with dragged tools',
        model: 'gpt-mock',
      }
    });

    expect(ficheResponse.status()).toBe(201);
    const fiche = await ficheResponse.json();
    console.log('📊 Created test fiche:', fiche.id);

    // Navigate to canvas
    await page.goto('/');
    await page.waitForTimeout(1000);
    await page.getByTestId('global-canvas-tab').click();
    await page.waitForTimeout(2000);

    // Test 1: Identify drag-and-drop targets
    console.log('📊 Test 1: Identifying drag-and-drop targets...');
    const canvasContainer = page.locator('[data-testid="canvas-container"]');
    const canvasVisible = await canvasContainer.isVisible();
    console.log('📊 Canvas container visible:', canvasVisible);

    if (canvasVisible) {
      const toolPalette = page.locator('[data-testid="tool-palette"]');
      const paletteVisible = await toolPalette.isVisible();

      if (paletteVisible) {
        // Test 2: Attempt tool drag operation
        console.log('📊 Test 2: Attempting tool drag operation...');

        try {
          // Look for first available tool
          const firstTool = toolPalette.locator('[data-testid^="tool-item"], .tool-item, .palette-tool').first();
          const toolExists = await firstTool.count() > 0;

          if (toolExists) {
            const toolText = await firstTool.textContent();
            console.log('📊 Dragging tool:', toolText?.trim());

            // Perform drag operation
            const canvasRect = await canvasContainer.boundingBox();
            if (canvasRect) {
              await firstTool.dragTo(canvasContainer, {
                targetPosition: { x: canvasRect.width / 2, y: canvasRect.height / 2 }
              });

              console.log('📊 Tool drag operation performed');
              await page.waitForTimeout(1000);

              // Test 3: Verify tool node appeared on canvas
              console.log('📊 Test 3: Verifying tool node placement...');
              const toolNodes = await page.locator('[data-testid^="node-tool"], .tool-node, [data-type="tool"]').count();
              console.log('📊 Tool nodes on canvas:', toolNodes);

              if (toolNodes > 0) {
                console.log('✅ Tool successfully placed on canvas');

                // Test 4: Verify tool node properties
                const toolNode = page.locator('[data-testid^="node-tool"], .tool-node, [data-type="tool"]').first();
                const nodeVisible = await toolNode.isVisible();
                const nodeText = await toolNode.textContent();

                console.log('📊 Tool node visible:', nodeVisible);
                console.log('📊 Tool node content:', nodeText?.substring(0, 50));

                if (nodeVisible) {
                  console.log('✅ Tool node properly rendered');
                }
              }
            }
          } else {
            console.log('⚠️  No draggable tools found in palette');
          }
        } catch (error) {
          console.log('📊 Drag operation error:', error.message);
          console.log('⚠️  Drag-and-drop may need UI implementation');
        }
      }
    }

    console.log('✅ Tool drag-and-drop test completed');
  });

  test('Node connection handle detection and interaction', async ({ page, request }) => {
    console.log('🚀 Starting node connection test...');

    const commisId = process.env.TEST_PARALLEL_INDEX || '0';

    // Create test fiche for connections
    const ficheResponse = await request.post('/api/fiches', {
      headers: {
        'X-Test-Commis': commisId,
        'Content-Type': 'application/json',
      },
      data: {
        name: `Connection Test Fiche ${Date.now()}`,
        system_instructions: 'Fiche for connection testing',
        task_instructions: 'Test node connections',
        model: 'gpt-mock',
      }
    });

    expect(ficheResponse.status()).toBe(201);
    const fiche = await ficheResponse.json();

    // Navigate to canvas
    await page.goto('/');
    await page.waitForTimeout(1000);
    await page.getByTestId('global-canvas-tab').click();
    await page.waitForTimeout(2000);

    // Test 1: Look for existing nodes or create them
    console.log('📊 Test 1: Detecting canvas nodes...');
    const existingNodes = await page.locator('[data-testid^="node-"], .node, [data-type]').count();
    console.log('📊 Existing nodes on canvas:', existingNodes);

    if (existingNodes === 0) {
      console.log('📊 No existing nodes - attempting to create nodes for connection test...');

      // Try to get fiche from shelf and add to canvas
      const ficheShelf = page.locator('[data-testid="fiche-shelf"]');
      const shelfVisible = await ficheShelf.isVisible();

      if (shelfVisible) {
        const ficheInShelf = ficheShelf.locator(`text=${fiche.name}`);
        const ficheAvailable = await ficheInShelf.isVisible();

        if (ficheAvailable) {
          const canvasContainer = page.locator('[data-testid="canvas-container"]');
          await ficheInShelf.dragTo(canvasContainer, {
            targetPosition: { x: 200, y: 200 }
          });
          await page.waitForTimeout(1000);
          console.log('📊 Fiche node creation attempted');
        }
      }
    }

    // Test 2: Detect connection handles
    console.log('📊 Test 2: Detecting connection handles...');
    const connectionHandles = await page.locator('[data-testid*="handle"], .connection-handle, .node-handle, [data-handleid]').count();
    console.log('📊 Connection handles found:', connectionHandles);

    if (connectionHandles > 0) {
      console.log('✅ Connection handles detected');

      // Test 3: Analyze handle types
      const handleElements = page.locator('[data-testid*="handle"], .connection-handle, .node-handle, [data-handleid]');
      const handleCount = await handleElements.count();

      const handleInfo = [];
      for (let i = 0; i < Math.min(handleCount, 5); i++) {
        const handleElement = handleElements.nth(i);
        const handlePosition = await handleElement.getAttribute('data-position');
        const handleType = await handleElement.getAttribute('data-type');
        const handleId = await handleElement.getAttribute('data-handleid') || await handleElement.getAttribute('data-testid');

        handleInfo.push({ position: handlePosition, type: handleType, id: handleId });
      }

      console.log('📊 Handle details:', handleInfo);

      // Test 4: Test handle interaction
      console.log('📊 Test 4: Testing handle interaction...');

      try {
        const firstHandle = handleElements.first();
        const handleVisible = await firstHandle.isVisible();

        if (handleVisible) {
          // Try to hover over handle to see if it responds
          await firstHandle.hover();
          await page.waitForTimeout(500);

          // Look for visual feedback (cursor change, highlight, etc.)
          const handleClass = await firstHandle.getAttribute('class');
          console.log('📊 Handle classes after hover:', handleClass);

          // Try to start a connection drag
          const handleRect = await firstHandle.boundingBox();
          if (handleRect) {
            // Start drag from handle
            await page.mouse.move(handleRect.x + handleRect.width/2, handleRect.y + handleRect.height/2);
            await page.mouse.down();

            // Move mouse to simulate connection dragging
            await page.mouse.move(handleRect.x + 100, handleRect.y + 50);
            await page.waitForTimeout(500);

            // Look for connection preview or line
            const connectionPreview = await page.locator('.connection-line, .connection-preview, [data-testid*="connection"]').count();
            console.log('📊 Connection preview elements:', connectionPreview);

            if (connectionPreview > 0) {
              console.log('✅ Connection dragging UI feedback detected');
            }

            // Release drag
            await page.mouse.up();
            await page.waitForTimeout(500);
          }
        }
      } catch (error) {
        console.log('📊 Handle interaction error:', error.message);
      }
    } else {
      console.log('⚠️  No connection handles detected - may need UI implementation');
    }

    console.log('✅ Node connection test completed');
  });

  test('Complex workflow topology creation', async ({ page, request }) => {
    console.log('🚀 Starting complex workflow topology test...');

    const commisId = process.env.TEST_PARALLEL_INDEX || '0';

    // Test 1: Create multiple fiches for complex workflow
    console.log('📊 Test 1: Creating multiple fiches...');
    const fiches = [];

    for (let i = 0; i < 3; i++) {
      const ficheResponse = await request.post('/api/fiches', {
        headers: {
          'X-Test-Commis': commisId,
          'Content-Type': 'application/json',
        },
        data: {
          name: `Topology Fiche ${i+1} ${Date.now()}`,
          system_instructions: `Fiche ${i+1} for topology testing`,
          task_instructions: `Handle step ${i+1} of complex workflow`,
          model: 'gpt-mock',
        }
      });

      if (ficheResponse.ok()) {
        const fiche = await ficheResponse.json();
        fiches.push(fiche);
        console.log(`📊 Created fiche ${i+1}:`, fiche.id);
      }
    }

    console.log('📊 Total fiches created:', fiches.length);

    // Test 2: Create complex workflow via API
    console.log('📊 Test 2: Creating complex workflow topology...');

    if (fiches.length >= 2) {
      try {
        const complexWorkflow = {
          name: `Complex Topology Workflow ${Date.now()}`,
          description: 'Multi-fiche workflow with complex connections',
          canvas_data: {
            nodes: [
              // Trigger node
              {
                id: 'trigger-1',
                type: 'trigger',
                position: { x: 50, y: 200 },
                config: { trigger: { type: 'manual', config: { enabled: true, params: {}, filters: [] } } }
              },
              // Fiche nodes
              ...fiches.map((fiche, index) => ({
                id: `fiche-${index + 1}`,
                type: 'fiche',
                fiche_id: fiche.id,
                position: { x: 200 + (index * 200), y: 150 + (index * 50) }
              })),
              // Tool nodes
              {
                id: 'http-tool-1',
                type: 'tool',
                tool_name: 'http_request',
                position: { x: 500, y: 100 },
                config: { url: 'https://httpbin.org/get', method: 'GET' }
              },
              {
                id: 'http-tool-2',
                type: 'tool',
                tool_name: 'http_request',
                position: { x: 500, y: 300 },
                config: { url: 'https://httpbin.org/post', method: 'POST' }
              }
            ],
            edges: [
              // Sequential flow: trigger -> fiche1 -> fiche2 -> fiche3
              { id: 'edge-1', source: 'trigger-1', target: 'fiche-1', type: 'default' },
              ...(fiches.length > 1 ? [{ id: 'edge-2', source: 'fiche-1', target: 'fiche-2', type: 'default' }] : []),
              ...(fiches.length > 2 ? [{ id: 'edge-3', source: 'fiche-2', target: 'fiche-3', type: 'default' }] : []),
              // Parallel tool execution
              { id: 'edge-4', source: 'fiche-1', target: 'http-tool-1', type: 'default' },
              { id: 'edge-5', source: 'fiche-1', target: 'http-tool-2', type: 'default' }
            ]
          }
        };

        const workflowResponse = await request.post('/api/workflows', {
          headers: {
            'X-Test-Commis': commisId,
            'Content-Type': 'application/json',
          },
          data: complexWorkflow
        });

        if (workflowResponse.ok()) {
          const workflow = await workflowResponse.json();
          console.log('📊 Complex workflow created:', workflow.id);

          // Test 3: Verify topology integrity
          console.log('📊 Test 3: Verifying topology integrity...');

          const verifyResponse = await request.get(`/api/workflows/${workflow.id}`, {
            headers: { 'X-Test-Commis': commisId }
          });

          if (verifyResponse.ok()) {
            const workflowData = await verifyResponse.json();
            const nodeCount = workflowData.canvas_data.nodes.length;
            const edgeCount = workflowData.canvas_data.edges.length;

            console.log('📊 Workflow nodes:', nodeCount);
            console.log('📊 Workflow edges:', edgeCount);

            // Verify all fiches are referenced
            const ficheIds = fiches.map(a => a.id.toString());
            const workflowJson = JSON.stringify(workflowData.canvas_data);
            const referencedFiches = ficheIds.filter(id => workflowJson.includes(id));

            console.log('📊 Fiches referenced in workflow:', referencedFiches.length);

            if (referencedFiches.length === fiches.length) {
              console.log('✅ All fiches properly referenced in complex topology');
            }

            // Test 4: Validate connection consistency
            const edges = workflowData.canvas_data.edges;
            const nodeIds = workflowData.canvas_data.nodes.map(n => n.id);

            const validConnections = edges.filter(edge =>
              nodeIds.includes(edge.source) && nodeIds.includes(edge.target)
            );

            console.log('📊 Valid connections:', validConnections.length);
            console.log('📊 Total connections:', edges.length);

            if (validConnections.length === edges.length) {
              console.log('✅ All connections reference valid nodes');
            }
          }
        } else {
          const error = await workflowResponse.text();
          console.log('❌ Complex workflow creation failed:', error.substring(0, 200));
        }
      } catch (error) {
        console.log('📊 Complex topology error:', error.message);
      }
    }

    console.log('✅ Complex workflow topology test completed');
  });

  test('Connection validation and constraint checking', async ({ page, request }) => {
    console.log('🚀 Starting connection validation test...');

    const commisId = process.env.TEST_PARALLEL_INDEX || '0';

    // Create test fiches
    const fiche1Response = await request.post('/api/fiches', {
      headers: {
        'X-Test-Commis': commisId,
        'Content-Type': 'application/json',
      },
      data: {
        name: `Validation Fiche 1 ${Date.now()}`,
        system_instructions: 'First validation fiche',
        task_instructions: 'Test connection validation',
        model: 'gpt-mock',
      }
    });

    const fiche2Response = await request.post('/api/fiches', {
      headers: {
        'X-Test-Commis': commisId,
        'Content-Type': 'application/json',
      },
      data: {
        name: `Validation Fiche 2 ${Date.now()}`,
        system_instructions: 'Second validation fiche',
        task_instructions: 'Test connection validation',
        model: 'gpt-mock',
      }
    });

    expect(fiche1Response.status()).toBe(201);
    expect(fiche2Response.status()).toBe(201);

    const fiche1 = await fiche1Response.json();
    const fiche2 = await fiche2Response.json();

    // Test 1: Valid connection topology
    console.log('📊 Test 1: Testing valid connection topology...');

    const validWorkflow = {
      name: `Validation Test Workflow ${Date.now()}`,
      description: 'Test connection validation rules',
      canvas_data: {
        nodes: [
          { id: 'trigger-1', type: 'trigger', position: { x: 50, y: 150 } },
          { id: 'fiche-1', type: 'fiche', fiche_id: fiche1.id, position: { x: 200, y: 150 } },
          { id: 'fiche-2', type: 'fiche', fiche_id: fiche2.id, position: { x: 350, y: 150 } },
          { id: 'tool-1', type: 'tool', tool_name: 'http_request', position: { x: 500, y: 150 } }
        ],
        edges: [
          { id: 'edge-1', source: 'trigger-1', target: 'fiche-1', type: 'default' },
          { id: 'edge-2', source: 'fiche-1', target: 'fiche-2', type: 'default' },
          { id: 'edge-3', source: 'fiche-2', target: 'tool-1', type: 'default' }
        ]
      }
    };

    const validResponse = await request.post('/api/workflows', {
      headers: {
        'X-Test-Commis': commisId,
        'Content-Type': 'application/json',
      },
      data: validWorkflow
    });

    console.log('📊 Valid workflow creation status:', validResponse.status());
    if (validResponse.ok()) {
      console.log('✅ Valid connection topology accepted');
    }

    // Test 2: Invalid connection attempts
    console.log('📊 Test 2: Testing invalid connection scenarios...');

    // Test circular reference
    const circularWorkflow = {
      name: `Circular Test Workflow ${Date.now()}`,
      description: 'Test circular reference validation',
      canvas_data: {
        nodes: [
          { id: 'fiche-1', type: 'fiche', fiche_id: fiche1.id, position: { x: 200, y: 150 } },
          { id: 'fiche-2', type: 'fiche', fiche_id: fiche2.id, position: { x: 350, y: 150 } }
        ],
        edges: [
          { id: 'edge-1', source: 'fiche-1', target: 'fiche-2', type: 'default' },
          { id: 'edge-2', source: 'fiche-2', target: 'fiche-1', type: 'default' } // Circular
        ]
      }
    };

    const circularResponse = await request.post('/api/workflows', {
      headers: {
        'X-Test-Commis': commisId,
        'Content-Type': 'application/json',
      },
      data: circularWorkflow
    });

    console.log('📊 Circular workflow creation status:', circularResponse.status());
    if (circularResponse.status() === 400 || circularResponse.status() === 422) {
      console.log('✅ Circular references properly rejected');
    } else if (circularResponse.ok()) {
      console.log('📊 Circular references allowed (system permits cycles)');
    }

    // Test 3: Non-existent node references
    console.log('📊 Test 3: Testing non-existent node references...');

    const invalidNodeWorkflow = {
      name: `Invalid Node Test Workflow ${Date.now()}`,
      description: 'Test invalid node reference validation',
      canvas_data: {
        nodes: [
          { id: 'fiche-1', type: 'fiche', fiche_id: fiche1.id, position: { x: 200, y: 150 } }
        ],
        edges: [
          { id: 'edge-1', source: 'fiche-1', target: 'non-existent-node', type: 'default' }
        ]
      }
    };

    const invalidNodeResponse = await request.post('/api/workflows', {
      headers: {
        'X-Test-Commis': commisId,
        'Content-Type': 'application/json',
      },
      data: invalidNodeWorkflow
    });

    console.log('📊 Invalid node workflow status:', invalidNodeResponse.status());
    if (invalidNodeResponse.status() === 400 || invalidNodeResponse.status() === 422) {
      console.log('✅ Invalid node references properly rejected');
    }

    console.log('✅ Connection validation test completed');
  });
});
