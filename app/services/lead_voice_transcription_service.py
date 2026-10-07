from app.providers.groq import GroqProvider, GroqTranscription


async def transcribe_lead_audio(
    provider: GroqProvider,
    *,
    filename: str,
    content_type: str,
    audio_bytes: bytes,
    model: str,
) -> GroqTranscription:
    return await provider.transcribe_audio(
        filename=filename,
        content_type=content_type,
        audio_bytes=audio_bytes,
        model=model,
    )
