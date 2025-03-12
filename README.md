# twilio-bidirectional-rnd

## Documentation for Outbound Call Integration Using Twilio, OpenAI, and ElevenLabs

### Overview
This documentation outlines the integration process for making outbound calls using Twilio, connected to real-time conversational AI via OpenAI, and utilizing ElevenLabs for Text-to-Speech (TTS) services. The setup allows dynamic audio streaming over WebSockets and manages cloud storage of call recordings using AWS.

Please refer to the `stable-branch` as the code there runs fine - the main branch was modified for testing convoi later.

### Directory Structure
```
./
└── twilio-bidirectional-rnd
    └── ElevenLabs
        ├── aws_handler.py
        ├── main.py
        ├── twilio_service.py
        └── utils.py
```

### Outbound Call Connection Workflow

#### Step-by-Step Process
1. **Outbound Call Initiation (Twilio)**:
   - Twilio initiates calls via its API (`POST /twilio/outbound_call`).
   - Streams audio in real-time through WebSockets to the backend.

2. **Backend WebSocket Endpoint (FastAPI)**:
   - Backend receives Twilio audio streams through WebSocket endpoints (`/elevenlabs/media-stream`).
   - Establishes WebSocket connections with OpenAI for conversational AI interactions.

2. **Integration with OpenAI (Conversational AI)**:
   - Connects to OpenAI's real-time API (`gpt-4o-realtime-preview-2024-10-01`) via WebSocket.
   - Sends audio streams received from Twilio to OpenAI in base64-encoded chunks.
   - Receives AI-generated conversational text responses in real-time.

3. **ElevenLabs Integration (Text-to-Speech)**:
   - Conversational AI responses from OpenAI are processed and converted into speech using ElevenLabs TTS.
   - Agents are dynamically created in ElevenLabs with custom prompts, enabling contextualized and concise interactions.

3. **Audio Handling and Streaming**:
   - Utilizes `TwilioAudioInterface` for audio management, mixing AI-generated audio with optional background noise.
   - Ensures real-time audio streaming back to Twilio via WebSockets.

4. **Recording Management (AWS S3)**:
   - Call recordings are initially downloaded from Twilio.
   - Recordings are securely uploaded to AWS S3 (`callspro-dev-private`) for storage and further analysis.
   - Ensures recordings are deleted from Twilio post-upload to enhance security.

### Environment Variables Configuration
Secure storage of keys and configurations is done using `.env`:
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- `ACCOUNT_SID`, `AUTH_TOKEN` (Twilio)
- `ELEVENLABS_API_KEY`
- `OPENAI_API_KEY`
- `AGENT_ID`, `BASE_URL`

### Utility Functions
- **`parse_time_to_utc_plus_5`** (in `utils.py`):
  - Converts timestamps from Twilio responses to the UTC+5 timezone.

### Error Handling and Logging
- Comprehensive error handling is implemented throughout the integration points (Twilio, OpenAI, ElevenLabs).
- Logs significant events such as call initiation, AI responses, audio streaming status, recording upload status, and error occurrences for debugging and monitoring.
