import os
import wave
import assemblyai as aai
import tempfile
from threading import Event

class AssemblyAIClient:
    def __init__(self, api_key: str):
        aai.settings.api_key = api_key
        self.transcriber = aai.Transcriber()

        # Attributes for realtime streaming
        self.realtime_transcriber = None
        self.final_transcripts = []
        self.partial_transcript = ""
        self.session_opened = False
        self.session_ended = False

        # Buffer for audio chunks (now 16kHz PCM S16LE)
        self.audio_chunks = []
        self.streaming_started = False
        self.session_open_event = Event()

    def transcribe_audio(self, audio_data: bytes) -> str:
        """
        Offline transcription of raw PCM audio data (8kHz, 16-bit mono).
        """
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            wav_path = tmp_wav.name
            with wave.open(tmp_wav, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)     # 16-bit
                wf.setframerate(8000)  # 8kHz
                wf.writeframes(audio_data)

        try:
            transcript = self.transcriber.transcribe(wav_path)
            return transcript.text
        finally:
            try:
                os.remove(wav_path)
            except OSError:
                pass

    # ------------------ Realtime Transcription Methods ------------------ #

    def on_open(self, session_opened: aai.RealtimeSessionOpened):
        self.session_opened = True
        print("AssemblyAI Realtime: Session ID:", session_opened.session_id)
        self.session_open_event.set()

    def on_data(self, transcript: aai.RealtimeTranscript):
        print("on_data called with transcript:", transcript)
        if not transcript.text:
            print("No text in transcript. Possibly silence or unrecognized audio.")
            return
        
        if isinstance(transcript, aai.RealtimeFinalTranscript):
            self.final_transcripts.append(transcript.text)
            print("Final:", transcript.text)
        else:
            self.partial_transcript = transcript.text
            print("Partial:", transcript.text)

    def on_error(self, error: aai.RealtimeError):
        print("AssemblyAI Realtime Error:", error)

    def on_close(self):
        self.session_ended = True
        print("AssemblyAI Realtime: Closing Session")

    def start_realtime_transcription_session(self):
        """
        Start a realtime transcription session with 16kHz, PCM S16LE encoding.
        We'll store partial and final transcripts and finalize after stopping.
        """
        # Reset state
        self.final_transcripts = []
        self.partial_transcript = ""
        self.session_opened = False
        self.session_ended = False
        self.audio_chunks = []
        self.streaming_started = False
        self.session_open_event.clear()

        self.realtime_transcriber = aai.RealtimeTranscriber(
            sample_rate=16000,
            on_data=self.on_data,
            on_error=self.on_error,
            on_open=self.on_open,
            on_close=self.on_close,
            encoding=aai.AudioEncoding.pcm_s16le,
            disable_partial_transcripts=False
        )
        self.realtime_transcriber.connect()

    def add_audio_chunk(self, audio_data: bytes):
        """
        Add a chunk of linear PCM (16kHz, 16-bit mono) audio to the buffer.
        These chunks will be streamed once session is open and we start streaming.
        """
        if not self.realtime_transcriber or self.session_ended:
            return
        self.audio_chunks.append(audio_data)

    def audio_generator(self):
        for i, chunk in enumerate(self.audio_chunks):
            print(f"Audio generator yielding chunk {i}, size={len(chunk)} bytes")
            yield chunk

    def start_streaming_audio(self):
        """
        Start streaming the audio from self.audio_chunks to AssemblyAI once the session is open.
        If session isn't open yet, wait for it.
        """
        if not self.realtime_transcriber or self.session_ended:
            print("Cannot start streaming: no active realtime session.")
            return

        # Wait until session is open before streaming
        print("Waiting for AssemblyAI session to open before streaming...")
        self.session_open_event.wait(timeout=5)  # Wait up to 5 seconds for session

        if not self.session_opened:
            print("Session never opened, cannot stream.")
            return

        if self.streaming_started:
            print("Already streaming audio.")
            return

        self.streaming_started = True
        # Start streaming and block until we run out of chunks
        self.realtime_transcriber.stream(self.audio_generator())

    def stop_realtime_transcription(self):
        """
        Stop sending audio and close the realtime transcriber session.
        Returns the final transcripts as a single string.
        """
        if self.realtime_transcriber and not self.session_ended:
            self.realtime_transcriber.close()

        # Return final transcripts as a single string
        return "\n".join(self.final_transcripts)
