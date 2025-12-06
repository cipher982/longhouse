#!/usr/bin/env node

/**
 * Enhanced AI Analysis with Focus on Functional Elements
 */

import OpenAI from 'openai';
import fs from 'fs';

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

async function enhancedAnalysis() {
  console.log('🔍 Running Enhanced AI Analysis with Focus on Functional Elements...');

  const rustScreenshot = fs.readFileSync('visual-reports/rust-dashboard-1758991999662.png');
  const reactScreenshot = fs.readFileSync('visual-reports/react-dashboard-1758992002283.png');

  const enhancedPrompt = `
CRITICAL ANALYSIS REQUIRED: Compare these dashboard screenshots with EXTREME attention to functional elements.

**FOCUS AREAS (High Priority):**

1. **ACTION BUTTONS & INTERACTIVE ELEMENTS**
   - Look at the "Actions" column in the table
   - Count and identify EVERY button/icon in each row
   - Are buttons visible vs invisible vs missing entirely?
   - What specific icons/buttons exist in one but not the other?

2. **TABLE ROW FUNCTIONALITY**
   - Does each table row have the same interactive elements?
   - Are there hover states, buttons, or controls that should be present?
   - Compare icon visibility, button placement, and functionality access

3. **MISSING INTERACTIVE FEATURES**
   - What can users DO in the Rust UI that they cannot do in React UI?
   - Are there controls, buttons, or interactive elements completely absent?

4. **FUNCTIONAL COMPLETENESS**
   - Can users perform the same actions in both UIs?
   - What workflows are broken or inaccessible in the React version?

**ANALYSIS METHODOLOGY:**
- Examine EACH TABLE ROW individually
- Count visible interactive elements per row
- Identify specific missing buttons by their icons/functions
- Focus on FUNCTIONALITY gaps, not just styling

**CRITICAL QUESTION:**
If a user needs to manage agents, what actions can they take in Rust UI vs React UI?

Be extremely specific about missing interactive elements and their impact on user workflows.
`;

  try {
    const response = await openai.chat.completions.create({
      model: "gpt-4o",
      messages: [
        {
          role: "user",
          content: [
            { type: "text", text: enhancedPrompt },
            { type: "text", text: "RUST UI (Full Functionality):" },
            {
              type: "image_url",
              image_url: {
                url: `data:image/png;base64,${rustScreenshot.toString('base64')}`,
                detail: "high"
              }
            },
            { type: "text", text: "REACT UI (Missing Functionality?):" },
            {
              type: "image_url",
              image_url: {
                url: `data:image/png;base64,${reactScreenshot.toString('base64')}`,
                detail: "high"
              }
            }
          ]
        }
      ],
      max_tokens: 3000,
      temperature: 0.1
    });

    const analysis = response.choices[0].message.content;

    // Save enhanced report
    const reportPath = `visual-reports/ENHANCED-analysis-${new Date().toISOString().replace(/[:.]/g, '-')}.md`;
    const report = `# 🎯 ENHANCED AI Visual Analysis - Functional Focus

**Generated**: ${new Date().toISOString()}
**Focus**: Missing Interactive Elements & Functionality Gaps
**Token Usage**: ${JSON.stringify(response.usage, null, 2)}

## Critical Functional Analysis

${analysis}

---
*Enhanced Analysis by AI Visual Testing Framework*
`;

    fs.writeFileSync(reportPath, report);
    console.log(`📄 Enhanced report saved: ${reportPath}`);

    // Show preview focusing on action buttons
    console.log('\n🚨 ENHANCED Analysis Preview:');
    console.log('═'.repeat(70));
    console.log(analysis);
    console.log('═'.repeat(70));

    return { analysis, reportPath, usage: response.usage };

  } catch (error) {
    console.error('❌ Enhanced analysis failed:', error.message);
    throw error;
  }
}

// Run enhanced analysis
enhancedAnalysis()
  .then(result => {
    console.log(`\n✅ Enhanced analysis complete!`);
    console.log(`📄 Full report: ${result.reportPath}`);
    console.log(`💰 Tokens: ${result.usage?.total_tokens}`);
  })
  .catch(console.error);
