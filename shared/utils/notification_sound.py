"""Add commentMore actions
Notification sounds for Wan2GP video generation application
Pure Python audio notification system with multiple backend support
"""

import os
import sys
import threading
import time
import numpy as np
import os
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

def generate_notification_beep(volume=50, sample_rate=44100):
    """Generate pleasant C major chord notification sound"""
    if volume == 0:
        return np.array([])
    
    volume = max(0, min(100, volume))
    
    # Volume curve mapping: 25%->50%, 50%->75%, 75%->100%, 100%->105%
    if volume <= 25:
        volume_mapped = (volume / 25.0) * 0.5
    elif volume <= 50:
        volume_mapped = 0.5 + ((volume - 25) / 25.0) * 0.25
    elif volume <= 75:
        volume_mapped = 0.75 + ((volume - 50) / 25.0) * 0.25
    else:
        volume_mapped = 1.0 + ((volume - 75) / 25.0) * 0.05  # Only 5% boost instead of 15%
    
    volume = volume_mapped
    
    # C major chord frequencies
    freq_c = 261.63  # C4
    freq_e = 329.63  # E4  
    freq_g = 392.00  # G4
    
    duration = 0.8
    t = np.linspace(0, duration, int(sample_rate * duration), False)
    
    # Generate chord components
    wave_c = np.sin(freq_c * 2 * np.pi * t) * 0.4
    wave_e = np.sin(freq_e * 2 * np.pi * t) * 0.3
    wave_g = np.sin(freq_g * 2 * np.pi * t) * 0.2
    
    wave = wave_c + wave_e + wave_g
    
    # Prevent clipping
    max_amplitude = np.max(np.abs(wave))
    if max_amplitude > 0:
        wave = wave / max_amplitude * 0.8
    
    # ADSR envelope
    def apply_adsr_envelope(wave_data):
        length = len(wave_data)
        attack_time = int(0.2 * length)
        decay_time = int(0.1 * length)
        release_time = int(0.5 * length)
        
        envelope = np.ones(length)
        
        if attack_time > 0:
            envelope[:attack_time] = np.power(np.linspace(0, 1, attack_time), 3)
        
        if decay_time > 0:
            start_idx = attack_time
            end_idx = attack_time + decay_time
            envelope[start_idx:end_idx] = np.linspace(1, 0.85, decay_time)
        
        if release_time > 0:
            start_idx = length - release_time
            envelope[start_idx:] = 0.85 * np.exp(-4 * np.linspace(0, 1, release_time))
        
        return wave_data * envelope
    
    wave = apply_adsr_envelope(wave)
    
    # Simple low-pass filter
    def simple_lowpass_filter(signal, cutoff_ratio=0.8):
        window_size = max(3, int(len(signal) * 0.001))
        if window_size % 2 == 0:
            window_size += 1
        
        kernel = np.ones(window_size) / window_size
        padded = np.pad(signal, window_size//2, mode='edge')
        filtered = np.convolve(padded, kernel, mode='same')
        return filtered[window_size//2:-window_size//2]
    
    wave = simple_lowpass_filter(wave)
    
    # Add reverb effect
    if len(wave) > sample_rate // 4:
        delay_samples = int(0.12 * sample_rate)
        reverb = np.zeros_like(wave)
        reverb[delay_samples:] = wave[:-delay_samples] * 0.08
        wave = wave + reverb
    
    # Apply volume first, then normalize to prevent clipping
    wave = wave * volume * 0.5
    
    # Final normalization with safety margin
    max_amplitude = np.max(np.abs(wave))
    if max_amplitude > 0.85:  # If approaching clipping threshold
        wave = wave / max_amplitude * 0.85  # More conservative normalization
    
    return wave
_mixer_lock = threading.Lock()

def play_audio_with_pygame(audio_data, sample_rate=44100):
    """
    Play audio with clean stereo output - sounds like single notification from both speakers
    """
    try:
        import pygame
        
        with _mixer_lock:
            if len(audio_data) == 0:
                return False
            
            # Clean mixer initialization - quit any existing mixer first
            if pygame.mixer.get_init() is not None:
                pygame.mixer.quit()
                time.sleep(0.2)  # Longer pause to ensure clean shutdown
            
            # Initialize fresh mixer
            pygame.mixer.pre_init(
                frequency=sample_rate,
                size=-16,
                channels=2,
                buffer=512  # Smaller buffer to reduce latency/doubling
            )
            pygame.mixer.init()
            
            # Verify clean initialization
            mixer_info = pygame.mixer.get_init()
            if mixer_info is None or mixer_info[2] != 2:
                return False
            
            # Prepare audio - ensure clean conversion
            audio_int16 = (audio_data * 32767).astype(np.int16)
            if len(audio_int16.shape) > 1:
                audio_int16 = audio_int16.flatten()
            
            # Create clean stereo with identical channels
            stereo_data = np.zeros((len(audio_int16), 2), dtype=np.int16)
            stereo_data[:, 0] = audio_int16  # Left channel
            stereo_data[:, 1] = audio_int16  # Right channel
            
            # Create sound and play once
            sound = pygame.sndarray.make_sound(stereo_data)
            
            # Ensure only one playback
            pygame.mixer.stop()  # Stop any previous sounds
            sound.play()
            
            # Wait for completion
            duration_ms = int(len(audio_data) / sample_rate * 1000) + 50
            pygame.time.wait(duration_ms)
            
            return True
        
    except ImportError:
        return False
    except Exception as e:
        print(f"Pygame clean error: {e}")
        return False
        
def play_audio_with_sounddevice(audio_data, sample_rate=44100):
    """Play audio using sounddevice backend"""
    try:
        import sounddevice as sd
        sd.play(audio_data, sample_rate)
        sd.wait()
        return True
        
    except ImportError:
        return False
    except Exception as e:
        print(f"Sounddevice error: {e}")
        return False


def play_audio_with_winsound(audio_data, sample_rate=44100):
    """Play audio using winsound backend (Windows only)"""
    if sys.platform != "win32":
        return False
        
    try:
        import winsound
        import wave
        import tempfile
        import uuid
        
        temp_dir = tempfile.gettempdir()
        temp_filename = os.path.join(temp_dir, f"notification_{uuid.uuid4().hex}.wav")
        
        try:
            with wave.open(temp_filename, 'w') as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                
                audio_int16 = (audio_data * 32767).astype(np.int16)
                wav_file.writeframes(audio_int16.tobytes())
            
            winsound.PlaySound(temp_filename, winsound.SND_FILENAME)
            
        finally:
            # Clean up temp file
            for _ in range(3):
                try:
                    if os.path.exists(temp_filename):
                        os.unlink(temp_filename)
                    break
                except:
                    time.sleep(0.1)
            
        return True
        
    except ImportError:
        return False
    except Exception as e:
        print(f"Winsound error: {e}")
        return False


def play_notification_sound(volume=50):
    """Play notification sound with specified volume"""
    if volume == 0:
        return
    
    audio_data = generate_notification_beep(volume=volume)
    
    if len(audio_data) == 0:
        return
    
    # Try audio backends in order
    audio_backends = [
        play_audio_with_pygame,
        play_audio_with_sounddevice,
        play_audio_with_winsound,
    ]
    
    for backend in audio_backends:
        try:
            if backend(audio_data):
                return
        except Exception as e:
            continue
    
    # Fallback: terminal beep
    print(f"All audio backends failed, using terminal beep")
    print('\a')


def play_notification_async(volume=50):
    """Play notification sound asynchronously (non-blocking)"""
    def play_sound():
        try:
            play_notification_sound(volume)
        except Exception as e:
            print(f"Error playing notification sound: {e}")
    
    sound_thread = threading.Thread(target=play_sound, daemon=True)
    sound_thread.start()


def notify_video_completion(video_path=None, volume=50):
    """Notify about completed video generation"""
    play_notification_async(volume)


if __name__ == "__main__":
    print("Testing notification sounds with different volumes...")
    print("Auto-detecting available audio backends...")
    
    volumes = [25, 50, 75, 100]
    for vol in volumes:
        print(f"Testing volume {vol}%:")
        play_notification_sound(vol)
        time.sleep(2)
    
    print("Test completed!")