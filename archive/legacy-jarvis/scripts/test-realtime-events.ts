#!/usr/bin/env npx tsx
/**
 * Standalone script to test OpenAI Realtime API event flow
 *
 * Run with: cd apps/jarvis && npx tsx scripts/test-realtime-events.ts
 *
 * This script:
 * 1. Gets a session token from zerg-backend
 * 2. Connects to OpenAI Realtime API using WebRTC
 * 3. Sends a text message
 * 4. Logs ALL events received
 * 5. Shows exactly what event structure the API returns
 *
 * PREREQUISITE: Start backend first via `make dev` or `make zerg`
 */

import { RealtimeSession, RealtimeAgent, OpenAIRealtimeWebRTC } from '@openai/agents/realtime';

const SERVER_URL = process.env.SERVER_URL || 'http://localhost:47300/api/jarvis';

async function getSessionToken(): Promise<string> {
  console.log(`🎫 Requesting session token from ${SERVER_URL}/session...`);
  const response = await fetch(`${SERVER_URL}/session`);
  if (!response.ok) {
    throw new Error(`Failed to get session token: ${response.status} ${response.statusText}`);
  }
  const data = await response.json() as any;
  return data.value || data.client_secret?.value;
}

async function main() {
  console.log('🚀 Starting Realtime API event test...\n');

  // Get token from server
  let token: string;
  try {
    token = await getSessionToken();
    console.log('✅ Got session token\n');
  } catch (error) {
    console.error('❌ Failed to get session token. Make sure Jarvis server is running.');
    console.error('   Start with: cd apps/jarvis && bun run dev:server');
    console.error('   Error:', error);
    process.exit(1);
  }

  // Create a simple agent
  const agent = new RealtimeAgent({
    name: 'test-agent',
    instructions: 'You are a helpful assistant. Keep responses short.',
  });

  // Create WebRTC transport (same as production)
  const transport = new OpenAIRealtimeWebRTC({
    // No media stream needed for text-only test
  });

  // Create session with model
  const session = new RealtimeSession(agent, {
    transport,
    model: 'gpt-4o-realtime-preview-2024-12-17',
  });

  // Track all events
  const events: Array<{ type: string; timestamp: number; data: any }> = [];

  // Listen to ALL transport events
  session.on('transport_event', (event: any) => {
    const t = event.type || 'unknown';

    // Skip high-frequency audio events
    if (t.includes('audio.delta') || t.includes('input_audio_buffer.append')) {
      return;
    }

    events.push({
      type: t,
      timestamp: Date.now(),
      data: event,
    });

    console.log(`📡 [${t}]`);

    // For text-related events, show more detail
    if (t.includes('text') || t.includes('transcript') || t.includes('content')) {
      console.log('   Data:', JSON.stringify({
        delta: event.delta,
        transcript: event.transcript,
        text: event.text,
        content: event.item?.content,
      }, null, 2).split('\n').map(l => '   ' + l).join('\n'));
    }

    // For item.done events, show the full content
    if (t === 'conversation.item.done') {
      console.log('   Item:', JSON.stringify({
        id: event.item?.id,
        role: event.item?.role,
        content: event.item?.content?.map((c: any) => ({
          type: c.type,
          text: c.text?.substring(0, 100),
          transcript: c.transcript?.substring(0, 100),
        })),
      }, null, 2).split('\n').map(l => '   ' + l).join('\n'));
    }

    // For response.done, show summary
    if (t === 'response.done') {
      console.log('   Response ID:', event.response?.id);
      console.log('   Output count:', event.response?.output?.length);
    }
  });

  // Connect using the session token
  console.log('🔗 Connecting to OpenAI Realtime API...');
  try {
    await session.connect({ apiKey: token });
    console.log('✅ Connected!\n');
  } catch (error) {
    console.error('❌ Failed to connect:', error);
    process.exit(1);
  }

  // Send a test message
  console.log('📤 Sending test message: "Say hello in exactly 3 words"');
  console.log('-----------------------------------------------------------\n');

  session.sendMessage('Say hello in exactly 3 words');

  // Wait for response (with timeout)
  await new Promise<void>((resolve) => {
    const timeout = setTimeout(() => {
      console.log('\n⏱️ Timeout - stopping event collection');
      resolve();
    }, 15000);

    // Also resolve when we see response.done
    const checkDone = setInterval(() => {
      if (events.some(e => e.type === 'response.done')) {
        clearInterval(checkDone);
        clearTimeout(timeout);
        // Wait a bit for any trailing events
        setTimeout(resolve, 1000);
      }
    }, 100);
  });

  // Disconnect
  console.log('\n🔌 Disconnecting...');
  await session.disconnect();

  // Summary
  console.log('\n===== EVENT SUMMARY =====');
  console.log(`Total events captured: ${events.length}`);
  console.log('\nEvent types received:');
  const typeCounts = events.reduce((acc, e) => {
    acc[e.type] = (acc[e.type] || 0) + 1;
    return acc;
  }, {} as Record<string, number>);
  Object.entries(typeCounts).forEach(([type, count]) => {
    console.log(`  ${type}: ${count}`);
  });

  // Show text-related events in detail
  const textEvents = events.filter(e =>
    e.type.includes('text') ||
    e.type.includes('transcript') ||
    e.type === 'conversation.item.done'
  );
  if (textEvents.length > 0) {
    console.log('\n===== TEXT/CONTENT EVENTS DETAIL =====');
    textEvents.forEach((e, i) => {
      console.log(`\n[${i + 1}] ${e.type}`);
      if (e.data.delta) console.log('  delta:', e.data.delta.substring(0, 50));
      if (e.data.transcript) console.log('  transcript:', e.data.transcript.substring(0, 50));
      if (e.data.text) console.log('  text:', e.data.text.substring(0, 50));
      if (e.data.item?.content) {
        e.data.item.content.forEach((c: any, j: number) => {
          console.log(`  content[${j}].type:`, c.type);
          if (c.text) console.log(`  content[${j}].text:`, c.text.substring(0, 50));
          if (c.transcript) console.log(`  content[${j}].transcript:`, c.transcript.substring(0, 50));
        });
      }
    });
  }

  console.log('\n✅ Test complete');
}

main().catch(console.error);
