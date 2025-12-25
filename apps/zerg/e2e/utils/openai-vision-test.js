#!/usr/bin/env node

/**
 * OpenAI Vision API Test Script
 *
 * Tests the OpenAI vision API with sample screenshots to validate the setup
 * before integrating with the visual testing framework.
 */

import OpenAI from 'openai';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Initialize OpenAI client
const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

/**
 * Convert image file to base64
 */
function imageToBase64(imagePath) {
  const imageBuffer = fs.readFileSync(imagePath);
  return imageBuffer.toString('base64');
}

/**
 * Test OpenAI vision API with a sample image
 */
async function testVisionAPI(imagePath, prompt) {
  try {
    console.log(`🔍 Testing vision API with: ${path.basename(imagePath)}`);
    console.log(`📝 Prompt: ${prompt}\n`);

    // Convert image to base64
    const base64Image = imageToBase64(imagePath);
    const mimeType = imagePath.endsWith('.png') ? 'image/png' : 'image/jpeg';

    // Make API call
    const response = await openai.chat.completions.create({
      model: "gpt-5.2", // Using gpt-4o for vision capabilities
      messages: [
        {
          role: "user",
          content: [
            {
              type: "text",
              text: prompt
            },
            {
              type: "image_url",
              image_url: {
                url: `data:${mimeType};base64,${base64Image}`,
                detail: "high" // Use high detail for better analysis
              }
            }
          ]
        }
      ],
      max_tokens: 1500
    });

    const analysis = response.choices[0].message.content;

    console.log("✅ API Response:");
    console.log("─".repeat(50));
    console.log(analysis);
    console.log("─".repeat(50));

    return {
      success: true,
      analysis,
      usage: response.usage
    };

  } catch (error) {
    console.error("❌ Vision API test failed:");
    console.error(error.message);
    return {
      success: false,
      error: error.message
    };
  }
}

/**
 * Test UI comparison analysis with two screenshots
 */
async function testUIComparison(rustScreenshot, reactScreenshot) {
  console.log("\n🔄 Testing UI Comparison Analysis...\n");

  const comparisonPrompt = `
Compare these two user interface screenshots. I need you to analyze the differences between them in detail.

Please provide:

1. **Overall Layout Differences**: Compare the general structure, spacing, and organization
2. **Visual Elements**: Differences in buttons, forms, navigation, colors, typography
3. **Content Alignment**: How elements are positioned relative to each other
4. **Style Inconsistencies**: Color schemes, font sizes, padding, margins
5. **Functionality Gaps**: Missing or extra features visible in either interface
6. **User Experience Impact**: How these differences might affect user interaction

Structure your response as:
- **Major Differences**: Most significant visual/structural differences
- **Minor Differences**: Small styling or positioning differences
- **Missing Elements**: Features present in one but not the other
- **Recommendations**: Specific changes needed to make them match

Be detailed and specific about pixel-level differences, color codes, spacing measurements where possible.
`;

  try {
    // Convert both images to base64
    const rustBase64 = imageToBase64(rustScreenshot);
    const reactBase64 = imageToBase64(reactScreenshot);

    const response = await openai.chat.completions.create({
      model: "gpt-5.2",
      messages: [
        {
          role: "user",
          content: [
            {
              type: "text",
              text: comparisonPrompt
            },
            {
              type: "text",
              text: "RUST UI (Target to match):"
            },
            {
              type: "image_url",
              image_url: {
                url: `data:image/png;base64,${rustBase64}`,
                detail: "high"
              }
            },
            {
              type: "text",
              text: "REACT UI (Needs to match Rust):"
            },
            {
              type: "image_url",
              image_url: {
                url: `data:image/png;base64,${reactBase64}`,
                detail: "high"
              }
            }
          ]
        }
      ],
      max_tokens: 2000
    });

    const comparison = response.choices[0].message.content;

    console.log("🎯 UI Comparison Analysis:");
    console.log("═".repeat(60));
    console.log(comparison);
    console.log("═".repeat(60));

    // Save comparison to file
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const reportPath = path.join(__dirname, '..', 'visual-reports', `ui-comparison-${timestamp}.md`);

    // Ensure reports directory exists
    const reportsDir = path.dirname(reportPath);
    if (!fs.existsSync(reportsDir)) {
      fs.mkdirSync(reportsDir, { recursive: true });
    }

    // Generate markdown report
    const report = `# UI Comparison Analysis
**Generated**: ${new Date().toISOString()}
**Rust Screenshot**: ${path.basename(rustScreenshot)}
**React Screenshot**: ${path.basename(reactScreenshot)}

## Analysis

${comparison}

## Technical Details
- **Model**: ${response.model}
- **Usage**: ${JSON.stringify(response.usage, null, 2)}
`;

    fs.writeFileSync(reportPath, report);
    console.log(`\n📄 Report saved: ${reportPath}`);

    return {
      success: true,
      comparison,
      reportPath,
      usage: response.usage
    };

  } catch (error) {
    console.error("❌ UI comparison test failed:");
    console.error(error.message);
    return {
      success: false,
      error: error.message
    };
  }
}

/**
 * Main test function
 */
async function main() {
  console.log("🚀 OpenAI Vision API Test\n");

  // Check for API key
  if (!process.env.OPENAI_API_KEY) {
    console.error("❌ Missing OPENAI_API_KEY environment variable");
    process.exit(1);
  }

  // Look for existing baseline screenshots
  const baselineDir = path.join(__dirname, '..', 'visual-baseline');
  const screenshotsExist = fs.existsSync(baselineDir);

  if (screenshotsExist) {
    const screenshots = fs.readdirSync(baselineDir).filter(f => f.endsWith('.png'));

    if (screenshots.length > 0) {
      console.log(`📸 Found ${screenshots.length} baseline screenshots\n`);

      // Test with first available screenshot
      const testImage = path.join(baselineDir, screenshots[0]);
      const testPrompt = "Describe this user interface screenshot in detail. What type of application is this? What are the main UI elements visible?";

      const result = await testVisionAPI(testImage, testPrompt);

      if (result.success) {
        console.log(`\n💰 API Usage: ${JSON.stringify(result.usage, null, 2)}`);
      }

      // If we have multiple screenshots, test UI comparison
      if (screenshots.length >= 2) {
        const rustScreenshot = path.join(baselineDir, screenshots[0]);
        const reactScreenshot = path.join(baselineDir, screenshots[1]);

        await testUIComparison(rustScreenshot, reactScreenshot);
      } else {
        console.log("\n⚠️  Need at least 2 screenshots to test UI comparison");
        console.log("   Run visual tests first to generate baseline screenshots");
      }
    } else {
      console.log("⚠️  No PNG screenshots found in visual-baseline directory");
    }
  } else {
    console.log("⚠️  visual-baseline directory not found");
    console.log("   Run visual tests first: npm test visual.spec.ts");
  }

  console.log("\n✅ Vision API test complete!");
}

// Run the test
main().catch(console.error);
