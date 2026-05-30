"""
Inference service for VibeVoice TTS.
Modified from inference_from_file.py to be suitable for service use.
Loads model once at startup and provides generate_audio() function for repeated calls.
"""
import os
import torch
import time
from typing import Optional

from vibevoice.modular.modeling_vibevoice_inference import VibeVoiceForConditionalGenerationInference
from vibevoice.processor.vibevoice_processor import VibeVoiceProcessor
from transformers.utils import logging

logging.set_verbosity_info()
logger = logging.get_logger(__name__)

# ============================================================
# VoiceMapper - same as inference_from_file.py
# ============================================================

class VoiceMapper:
    """Maps speaker names to voice file paths"""
    
    def __init__(self):
        self.setup_voice_presets()

        # Change name according to our preset wav file
        new_dict = {}
        for name, path in self.voice_presets.items():
            if '_' in name:
                name = name.split('_')[0]
            if '-' in name:
                name = name.split('-')[-1]
            new_dict[name] = path
        self.voice_presets.update(new_dict)

    def setup_voice_presets(self):
        """Setup voice presets by scanning the voices directory."""
        voices_dir = os.path.join(os.path.dirname(__file__), "voices")
        
        if not os.path.exists(voices_dir):
            print(f"Warning: Voices directory not found at {voices_dir}")
            self.voice_presets = {}
            self.available_voices = {}
            return
        
        self.voice_presets = {}
        wav_files = [f for f in os.listdir(voices_dir) 
                    if f.lower().endswith('.wav') and os.path.isfile(os.path.join(voices_dir, f))]
        
        for wav_file in wav_files:
            name = os.path.splitext(wav_file)[0]
            full_path = os.path.join(voices_dir, wav_file)
            self.voice_presets[name] = full_path
        
        self.voice_presets = dict(sorted(self.voice_presets.items()))
        self.available_voices = {
            name: path for name, path in self.voice_presets.items()
            if os.path.exists(path)
        }
        
        print(f"Found {len(self.available_voices)} voice files in {voices_dir}")

    def get_voice_path(self, speaker_name: str) -> str:
        """Get voice file path for a given speaker name"""
        if speaker_name in self.voice_presets:
            return self.voice_presets[speaker_name]
        
        speaker_lower = speaker_name.lower()
        for preset_name, path in self.voice_presets.items():
            if preset_name.lower() in speaker_lower or speaker_lower in preset_name.lower():
                return path
        
        default_voice = list(self.voice_presets.values())[0]
        print(f"Warning: No voice preset found for '{speaker_name}', using default voice: {default_voice}")
        return default_voice


# ============================================================
# Global model state (loaded once at startup)
# ============================================================

_model = None
_processor = None
_voice_mapper = None
_device = None


def initialize(model_path: str = None,
               device: Optional[str] = None,
               cfg_scale: float = 1.3,
               seed: Optional[int] = None):
    """
    Initialize the VibeVoice model, processor, and voice mapper.
    Call this once at startup before calling generate_audio().
    
    Args:
        model_path: HuggingFace model path or local directory
        device: 'cuda', 'mps', 'cpu', or auto-detect if None
        cfg_scale: CFG scale for generation
        seed: Random seed for reproducibility (optional)
    """
    global _model, _processor, _voice_mapper, _device
    
    # Auto-detect local model path
    if model_path is None:
        local_model_path = os.path.join(os.path.dirname(__file__), "..", "models", "VibeVoice-1.5b")
        if os.path.exists(local_model_path):
            model_path = local_model_path
            print(f"Using local model: {model_path}")
        else:
            model_path = "microsoft/VibeVoice-1.5b"
            print(f"Local model not found, using HuggingFace: {model_path}")
    
    # Auto-detect device if not specified
    if device is None:
        if torch.cuda.is_available():
            device = "cuda"
        elif torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
    
    # Normalize 'mpx' typo to 'mps'
    if device.lower() == "mpx":
        print("Note: device 'mpx' detected, treating it as 'mps'.")
        device = "mps"
    
    # Validate device availability
    if device == "mps" and not torch.backends.mps.is_available():
        print("Warning: MPS not available. Falling back to CPU.")
        device = "cpu"
    
    if device == "cuda" and not _cuda_runtime_usable():
        device = "cpu"
    
    _device = device
    print(f"Using device: {device}")
    
    if seed is not None:
        print(f"Setting seed: {seed}")
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    
    # Initialize voice mapper
    _voice_mapper = VoiceMapper()
    
    # Load processor
    print(f"Loading processor & model from {model_path}")
    _processor = VibeVoiceProcessor.from_pretrained(model_path)
    
    # Decide dtype & attention implementation
    if device == "mps":
        load_dtype = torch.float32
        attn_impl_primary = "sdpa"
    elif device == "cuda":
        load_dtype = torch.bfloat16
        attn_impl_primary = "flash_attention_2"
    else:  # cpu
        load_dtype = torch.float32
        attn_impl_primary = "sdpa"
    
    print(f"Using torch_dtype: {load_dtype}, attn_implementation: {attn_impl_primary}")
    
    # Load model
    try:
        if device == "mps":
            _model = VibeVoiceForConditionalGenerationInference.from_pretrained(
                model_path,
                torch_dtype=load_dtype,
                attn_implementation=attn_impl_primary,
                device_map=None,
            )
            _model.to("mps")
        elif device == "cuda":
            _model = VibeVoiceForConditionalGenerationInference.from_pretrained(
                model_path,
                torch_dtype=load_dtype,
                device_map="cuda",
                attn_implementation=attn_impl_primary,
            )
        else:  # cpu
            _model = VibeVoiceForConditionalGenerationInference.from_pretrained(
                model_path,
                torch_dtype=load_dtype,
                device_map="cpu",
                attn_implementation=attn_impl_primary,
            )
    except Exception as e:
        if attn_impl_primary == 'flash_attention_2':
            print(f"[ERROR] {type(e).__name__}: {e}")
            print("Error loading model with flash_attention_2. Falling back to SDPA.")
            _model = VibeVoiceForConditionalGenerationInference.from_pretrained(
                model_path,
                torch_dtype=load_dtype,
                device_map=(device if device in ("cuda", "cpu") else None),
                attn_implementation='sdpa'
            )
            if device == "mps":
                _model.to("mps")
        else:
            raise e
    
    _model.eval()
    _model.set_ddpm_inference_steps(num_steps=10)
    
    if hasattr(_model.model, 'language_model'):
        print(f"Language model attention: {_model.model.language_model.config._attn_implementation}")
    
    print("Model initialization complete.")


def generate_audio(text: str, language: str = "en", 
                   speaker_name: str = "Andrew",
                   output_path: str = None,
                   cfg_scale: float = 1.3) -> Optional[str]:
    """
    Generate audio from text using the pre-loaded VibeVoice model.
    
    Args:
        text: The text to convert to speech
        language: Language code ('en' or 'zh')
        speaker_name: Name of the speaker/voice to use
        output_path: Path to save the audio file (auto-generated if None)
        cfg_scale: CFG scale for generation
        
    Returns:
        Path to the generated audio file, or None on failure
    """
    if _model is None or _processor is None or _voice_mapper is None:
        print("Error: Model not initialized. Call initialize() first.")
        return None
    
    try:
        # Get voice path for speaker
        voice_path = _voice_mapper.get_voice_path(speaker_name)
        voice_samples = [voice_path]
        
        # Prepare text (fix smart quotes and add speaker prefix to each line)
        text = text.replace("’", "'")
        
        # Model requires "Speaker X:" prefix on each non-empty line
        lines = text.split('\n')
        processed_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("Speaker"):
                processed_lines.append(f"Speaker 1: {stripped}")
            elif stripped:
                processed_lines.append(stripped)
            else:
                processed_lines.append(line)  # Keep empty lines as-is
        text = '\n'.join(processed_lines)
        
        print(f"Generating audio for speaker '{speaker_name}', language '{language}'")
        print(f"Text preview: {text[:100]}...")
        
        # Prepare inputs
        inputs = _processor(
            text=[text],
            voice_samples=[voice_samples],
            padding=True,
            return_tensors="pt",
            return_attention_mask=True,
        )
        
        # Move tensors to target device
        target_device = _device if _device != "cpu" else "cpu"
        for k, v in inputs.items():
            if torch.is_tensor(v):
                inputs[k] = v.to(target_device)
        
        # Generate audio
        start_time = time.time()
        outputs = _model.generate(
            **inputs,
            max_new_tokens=None,
            cfg_scale=cfg_scale,
            tokenizer=_processor.tokenizer,
            generation_config={'do_sample': False},
            verbose=False,
            is_prefill=True,
        )
        generation_time = time.time() - start_time
        
        # Calculate metrics
        if outputs.speech_outputs and outputs.speech_outputs[0] is not None:
            sample_rate = 24000
            audio_samples = outputs.speech_outputs[0].shape[-1] if len(outputs.speech_outputs[0].shape) > 0 else len(outputs.speech_outputs[0])
            audio_duration = audio_samples / sample_rate
            rtf = generation_time / audio_duration if audio_duration > 0 else float('inf')
            
            print(f"Generated audio duration: {audio_duration:.2f} seconds, time: {generation_time:.2f}s, RTF: {rtf:.2f}x")
        else:
            print("No audio output generated")
            return None
        
        # Generate output path if not provided
        if output_path is None:
            import uuid
            output_dir = os.path.join(os.path.dirname(__file__), "generated_audio")
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, f"tts_{uuid.uuid4().hex}.wav")
        else:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # Save audio
        _processor.save_audio(
            outputs.speech_outputs[0],
            output_path=output_path,
        )
        
        print(f"Saved audio to {output_path}")
        return output_path
        
    except Exception as e:
        print(f"Error generating audio: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None


def _cuda_runtime_usable() -> bool:
    """Validate whether CUDA can actually execute kernels on this machine."""
    if not torch.cuda.is_available():
        return False
    
    try:
        device_index = torch.cuda.current_device()
        major, minor = torch.cuda.get_device_capability(device_index)
        required_arch = f"sm_{major}{minor}"
        supported_arches = torch.cuda.get_arch_list()
        if required_arch not in supported_arches:
            print(f"Warning: CUDA arch sm_{major}{minor} not supported. Falling back to CPU.")
            return False
        
        _ = torch.zeros(1, device="cuda") + 1
        return True
    except Exception as exc:
        print(f"Warning: CUDA runtime check failed ({type(exc).__name__}: {exc}). Falling back to CPU.")
        return False


# Allow standalone testing
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="VibeVoice Inference Service Test")
    parser.add_argument("--model_path", type=str, default="microsoft/VibeVoice-1.5b")
    parser.add_argument("--device", type=str, default=None)
    parser.add_argument("--text", type=str, default="Hello, this is a test of the VibeVoice inference service.")
    parser.add_argument("--speaker", type=str, default="Andrew")
    parser.add_argument("--output", type=str, default=None)
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("VibeVoice Inference Service - Standalone Test")
    print("=" * 60)
    
    initialize(model_path=args.model_path, device=args.device)
    
    result = generate_audio(
        text=args.text,
        speaker_name=args.speaker,
        output_path=args.output
    )
    
    if result:
        print(f"\nSuccess! Audio saved to: {result}")
    else:
        print("\nFailed to generate audio.")
