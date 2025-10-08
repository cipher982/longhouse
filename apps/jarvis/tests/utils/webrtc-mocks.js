// Mock utilities for WebRTC testing

export function setupWebRTCMocks(page) {
  return page.addInitScript(() => {
    class MockDataChannel {
      constructor(label) {
        this.label = label;
        this.readyState = 'connecting';
        this.onopen = null;
        this.onmessage = null;
        this.onclose = null;
        setTimeout(() => {
          this.readyState = 'open';
          if (this.onopen) this.onopen();
        }, 50);
      }
      send(data) {
        if (this.readyState === 'open') {
          setTimeout(() => {
            if (this.onmessage) {
              const mockResponse = {
                data: JSON.stringify({
                  type: 'response.final',
                  output: [{ content: [{ text: 'Mock AI response' }] }]
                })
              };
              this.onmessage(mockResponse);
            }
          }, 200);
        }
      }
      close() {
        this.readyState = 'closed';
        if (this.onclose) this.onclose();
      }
    }

    class MockRTCPeerConnection {
      constructor() {
        this.localDescription = null;
        this.remoteDescription = null;
        this.ontrack = null;
        this.connectionState = 'new';
        this.dataChannels = [];
      }
      async createOffer() {
        return { type: 'offer', sdp: 'mock-offer-sdp-v=0\r\no=- 123 456 IN IP4 127.0.0.1\r\n' };
      }
      async setLocalDescription(desc) {
        this.localDescription = desc;
      }
      async setRemoteDescription(desc) {
        this.remoteDescription = desc;
        setTimeout(() => {
          this.connectionState = 'connected';
          if (this.ontrack) {
            const mockStream = new MediaStream();
            this.ontrack({ streams: [mockStream] });
          }
        }, 100);
      }
      createDataChannel(label) {
        const channel = new MockDataChannel(label);
        this.dataChannels.push(channel);
        return channel;
      }
      addTrack(track, stream) {
        this._addedTracks = this._addedTracks || [];
        this._addedTracks.push({ track, stream });
      }
      close() {
        this.connectionState = 'closed';
        this.dataChannels.forEach(dc => dc.close());
      }
    }

    // Mock WebRTC APIs
    window.RTCPeerConnection = MockRTCPeerConnection;

    // Mock getUserMedia
    if (!navigator.mediaDevices) navigator.mediaDevices = {};
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      const stream = new MediaStream();
      if (constraints?.audio) {
        const audioTrack = new MediaStreamTrack();
        audioTrack.kind = 'audio';
        audioTrack.enabled = true;
        audioTrack.getSettings = () => ({ sampleRate: 48000 });
        stream.addTrack(audioTrack);
      }
      return stream;
    };

    // Mock AudioContext
    window.AudioContext = class MockAudioContext {
      constructor() { this.state = 'running'; }
      createMediaStreamSource() { return { connect: () => {} }; }
      createAnalyser() {
        return {
          fftSize: 256,
          getByteTimeDomainData: (data) => {
            for (let i = 0; i < data.length; i++) {
              data[i] = 128 + Math.sin(Date.now() / 1000) * 50;
            }
          }
        };
      }
    };
    window.webkitAudioContext = window.AudioContext;
  });
}
