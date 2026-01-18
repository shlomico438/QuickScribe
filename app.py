import os
import sys
import tempfile
import datetime
import requests
from docx import Document
import smtplib
import torch
import omegaconf
import subprocess
from email.message import EmailMessage

from docx.enum.text import WD_ALIGN_PARAGRAPH
from faster_whisper import WhisperModel
from whisperx.diarize import DiarizationPipeline
import warnings
import gc  # Important for memory management

warnings.filterwarnings("ignore", category=UserWarning)

MY_HF_TOKEN = "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"

# ==============================================================================
# 🔴 FIX: Whitelist ALL OmegaConf components blocking the load
# ==============================================================================
# 1. Force weights_only=False (Aggressive Monkey Patch)
original_load = torch.load


def unsafe_load(*args, **kwargs):
    # Forcefully overwrite the safety switch to False
    kwargs['weights_only'] = False
    return original_load(*args, **kwargs)


torch.load = unsafe_load

# 2. Whitelist the specific classes mentioned in your error errors
torch.serialization.add_safe_globals([
    omegaconf.listconfig.ListConfig,  # Blocked in error 1
    omegaconf.dictconfig.DictConfig,  # Often blocked
    omegaconf.base.ContainerMetadata  # <--- THIS IS THE NEW ONE BLOCKING YOU
])
# ==============================================================================
import whisperx
import warnings

warnings.filterwarnings("ignore", category=UserWarning)

# --- Configuration & Device Setup ---
device = "cuda" if torch.cuda.is_available() else "cpu"
compute_type = "float16" if device == "cuda" else "int8"

# Define model IDs
HEBREW_MODEL_ID = "ivrit-ai/faster-whisper-v2-d4"
MULTI_LANGUAGE_MODEL_ID = "large-v2" if device == "cuda" else "medium"

print(f"Running on device: {device}, compute_type: {compute_type}")

# ---------- CONFIG ----------
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
FROM_EMAIL = os.environ.get("FROM_EMAIL")

speaker_map = {
    "SPEAKER_00": "דובר 1",
    "SPEAKER_01": "דובר 2",
    "SPEAKER_02": "דובר 3",
    "SPEAKER_03": "דובר 4",
    "SPEAKER_04": "דובר 5",
    "SPEAKER_05": "דובר 6",
}


# ---------- INPUT HANDLING ----------
def get_inputs():
    # Environment variables for serverless mode
    customer_email = os.environ.get("CUSTOMER_EMAIL")
    zoom_url = os.environ.get("ZOOM_URL")
    local_file = os.environ.get("LOCAL_FILE")
    print(f"customer_email: {customer_email}")
    print(f"zoom_url: {zoom_url}")
    print(f"local_file: {local_file}")

    if device == "cuda":
        # Serverless / Production logic
        if customer_email and (zoom_url or local_file):
            return customer_email, zoom_url, local_file
        return None, None, None  # Should handle error

    else:
        return customer_email, zoom_url, local_file
    #     if local_file is None):
    #
    #         # --- LOCAL DEBUGGING MODE (CPU) ---
    #         print("--- DEBUG MODE: Auto-detecting file in /data ---")
    #
    #         # 1. Look for any audio/video file in the mapped /data folder
    #         data_folder = "/data"
    #         found_file = None
    #
    #         try:
    #             # We list files. Python handles the special characters better than manual string typing
    #             files = os.listdir(data_folder)
    #             for f in files:
    #                 if f.lower().endswith(('.mp3', '.mp4', '.wav', '.m4a')):
    #                     found_file = os.path.join(data_folder, f)
    #                     break
    #         except OSError:
    #             pass
    #
    #         if found_file:
    #             print(f"Found file: {found_file}")
    #
    #             # 2. RENAME to a safe ASCII name to avoid FFmpeg errors
    #             # We rename it to 'safe_input.mp4' (or keep extension) inside the container
    #             # This doesn't change the name on your Windows Desktop, only how Docker sees it (usually)
    #             # OR we make a copy. To be safe, let's just use the path found by os.listdir
    #
    #             # BETTER: Rename it to a safe temporary name to guarantee FFmpeg works
    #             safe_name = "safe_input" + os.path.splitext(found_file)[1]
    #             safe_path = os.path.join(data_folder, safe_name)
    #
    #             try:
    #                 if found_file != safe_path:
    #                     os.rename(found_file, safe_path)
    #                     print(f"Renamed '{found_file}' to '{safe_path}' for processing.")
    #                 local_file = safe_path
    #             except Exception as e:
    #                 print(f"Could not rename file (permissions?): {e}. Trying original path.")
    #                 local_file = found_file
    #         else:
    #             # Fallback if folder is empty
    #             local_file = None



if device == "cuda":
    CUSTOMER_EMAIL, ZOOM_URL, LOCAL_FILE = get_inputs()
else:
    # This calls the smart function above
    CUSTOMER_EMAIL, ZOOM_URL, LOCAL_FILE = get_inputs()

if not (ZOOM_URL or LOCAL_FILE):
    print({"status": "error", "message": "No input file provided"}, file=sys.stderr)
    sys.exit(1)

# ---------- STEP 1: Get audio & Convert to MP3 ----------
try:
    # 1. Get the file (Download or Local)
    if LOCAL_FILE:
        original_file_path = LOCAL_FILE
        print(f"Using local file: {original_file_path}")
    else:
        print(f"Downloading from Zoom URL: {ZOOM_URL}")
        response = requests.get(ZOOM_URL, stream=True)
        response.raise_for_status()
        # Save initially as whatever format it came in (usually mp4 for Zoom)
        temp_download = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        for chunk in response.iter_content(chunk_size=8192):
            temp_download.write(chunk)
        temp_download.close()
        original_file_path = temp_download.name
        print("Download complete.")

    # 2. Check and Convert to MP3 if needed
    # We define the path for the final MP3 file
    if original_file_path.lower().endswith(".mp4"):
        print("MP4 detected. Converting to MP3...")

        # Create a temp file for the MP3
        mp3_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        mp3_file.close()
        temp_audio_file_path = mp3_file.name

        # Run FFmpeg conversion
        # -i: input, -vn: disable video, -acodec libmp3lame: mp3 codec, -q:a 2: high quality VBR
        # -y: overwrite output if exists
        command = [
            "ffmpeg",
            "-i", original_file_path,
            "-vn",
            "-acodec", "libmp3lame",
            "-q:a", "2",
            "-y",
            temp_audio_file_path
        ]

        try:
            # Run the command and hide output unless there's an error
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            print(f"Conversion successful: {temp_audio_file_path}")

            # Optional: Delete the original MP4 if we downloaded it (save space)
            if not LOCAL_FILE and original_file_path != temp_audio_file_path:
                os.unlink(original_file_path)

        except subprocess.CalledProcessError as e:
            print(f"FFmpeg conversion failed: {e.stderr.decode()}", file=sys.stderr)
            # Fallback: try to use the original file if conversion fails
            temp_audio_file_path = original_file_path

    else:
        # If it's not MP4, just use it as is (or rename logic if needed)
        temp_audio_file_path = original_file_path
        print("File is not MP4 (or logic skipped), proceeding with original.")

except Exception as e:
    print({"status": "error", "message": f"Failed to get/convert audio: {e}"}, file=sys.stderr)
    sys.exit(1)

# ---------- STEP 2: Transcribe, Align & Diarize (Sequential Logic) ----------
try:
    print("Loading audio for analysis...")
    audio = whisperx.load_audio(temp_audio_file_path)

    # --- 2a. Detect Language (Fast) ---
    print("Detecting language...")
    # Load a standard model just for detection on a small chunk
    model_detect = whisperx.load_model(MULTI_LANGUAGE_MODEL_ID, device=device, compute_type=compute_type)

    # 30 seconds is enough for detection
    audio_segment = audio[:30 * 16000]
    result_detect = model_detect.transcribe(audio_segment, batch_size=1)
    detected_lang = result_detect["language"]
    print(f"Detected language: '{detected_lang}'")

    # Cleanup detection model to save VRAM
    del model_detect
    gc.collect()
    if device == 'cuda':
        torch.cuda.empty_cache()

    # --- 2b. Load the CORRECT Model & Transcribe Full Audio ---
    if detected_lang == "he":
        print(f"Language is Hebrew. Loading specialized model: {HEBREW_MODEL_ID}")
        # Load the specialized Hebrew model
        model = whisperx.load_model(HEBREW_MODEL_ID, device=device, compute_type=compute_type)
        language_arg = "he"
    else:
        print(f"Language is {detected_lang}. Loading multi-language model: {MULTI_LANGUAGE_MODEL_ID}")
        model = whisperx.load_model(MULTI_LANGUAGE_MODEL_ID, device=device, compute_type=compute_type)
        language_arg = detected_lang

    print("Starting full transcription...")
    # Transcribe the full audio ONCE with the chosen model
    result = model.transcribe(audio, language=language_arg, batch_size=16 if device == "cuda" else 4)

    # Free memory before alignment/diarization
    del model
    gc.collect()
    if device == 'cuda':
        torch.cuda.empty_cache()

    # --- 2c. Align (Critical for good Diarization mapping) ---
    print("Aligning transcript...")
    try:
        model_a, metadata = whisperx.load_align_model(language_code=language_arg, device=device)
        result = whisperx.align(result["segments"], model_a, metadata, audio, device, return_char_alignments=False)

        del model_a
        gc.collect()
        if device == 'cuda':
            torch.cuda.empty_cache()
    except Exception as e:
        print(f"Alignment warning (continuing without alignment): {e}")

    # --- 2d. Diarize (Identify Speakers) ---
    print("Identifying Speakers (Diarization)...")
    try:
        diarize_model = DiarizationPipeline(use_auth_token=MY_HF_TOKEN, device=device)
        diarize_segments = diarize_model(audio)

        # Merge speaker info into the transcript
        result = whisperx.assign_word_speakers(diarize_segments, result)
        print("Speakers assigned successfully.")

    except Exception as e:
        print(f"Diarization Warning (Transcription will proceed without speakers): {e}")

    # --- 2e. Format Output Text ---
    print("Formatting final transcript...")
    transcript_text = ""
    for segment in result['segments']:
        speaker_id = segment.get('speaker', 'Unknown')

        # Map SPEAKER_00 to "דובר 1" etc.
        if speaker_id in speaker_map:
            speaker_name = speaker_map[speaker_id]
        elif speaker_id == 'Unknown':
            speaker_name = ""
        else:
            speaker_name = speaker_id

        text = segment['text'].strip()
        if text:
            if speaker_name:
                transcript_text += f"{speaker_name}: {text}\n"
            else:
                transcript_text += f"{text}\n"

    print("Transcription complete.")

except Exception as e:
    import traceback

    traceback.print_exc()
    print({"status": "error", "message": f"Transcription failed: {e}"}, file=sys.stderr)

    if 'temp_audio_file_path' in locals() and not LOCAL_FILE and os.path.exists(temp_audio_file_path):
        os.unlink(temp_audio_file_path)
    sys.exit(1)

# ---------- STEP 3: Generate DOCX with UNIQUE Name ----------
try:
    print("Generating files...")
    doc = Document()

    text_to_write = transcript_text if 'transcript_text' in locals() else "Error: No transcript text found."
    # Determine title based on language
    if 'detected_lang' in locals() and detected_lang == 'he':
        heading_text = "תמלול"
    else:
        heading_text = "Transcript"

    # Add the heading and center it
    heading = doc.add_heading(heading_text, 0)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph = doc.add_paragraph(text_to_write)

    if 'detected_lang' in locals() and detected_lang == 'he':
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        paragraph.paragraph_format.bidi = True
    else:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        paragraph.paragraph_format.bidi = True

    # 1. Output directory
    if os.name == 'nt':  # Windows local
        output_dir = r"C:\Work\runpod\QuickScribe\Output"
    else:  # Linux Docker
        output_dir = "/tmp/QuickScribeOutput"

    os.makedirs(output_dir, exist_ok=True)

    # 2. Unique Filename
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base_filename = f"transcript_{timestamp}"

    # Save DOCX
    docx_path = os.path.join(output_dir, base_filename + ".docx")
    print(f"Saving DOCX to: {docx_path}...")
    doc.save(docx_path)

    # Save TXT
    txt_path = os.path.join(output_dir, base_filename + ".txt")
    print(f"Saving TXT to: {txt_path}...")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text_to_write.strip())

    print("Files saved successfully.")
    docx_file_path = docx_path

except Exception as e:
    print({"status": "error", "message": f"File generation failed: {e}"}, file=sys.stderr)
    sys.exit(1)


# ---------- STEP 4: Deliver output & Clean up ----------
def send_email(file_path, to_email):
    try:
        print(f"Sending email to {to_email}...")
        msg = EmailMessage()
        msg['Subject'] = 'Your Transcript'
        msg['From'] = FROM_EMAIL
        msg['To'] = to_email
        msg.set_content("Please find your transcript attached.")

        with open(file_path, 'rb') as f:
            file_data = f.read()

        msg.add_attachment(
            file_data,
            maintype='application',
            subtype='vnd.openxmlformats-officedocument.wordprocessingml.document',
            filename=os.path.basename(file_path)
        )

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
        print("Email sent successfully.")
        return True
    except Exception as e:
        print({"status": "error", "message": f"Email sending failed: {e}"}, file=sys.stderr)
        return False


if CUSTOMER_EMAIL:
    success = send_email(docx_file_path, CUSTOMER_EMAIL)

    if success:
        try:
            print(f"Cleaning up: Removing {docx_file_path}...")
            os.remove(docx_file_path)
            print("Cleanup successful.")
        except Exception as e:
            print(f"Warning: Failed to delete file after sending: {e}")
    else:
        print(f"Email failed. Keeping file at {docx_file_path} for manual inspection.")

else:
    print(f"No email provided. Transcript saved locally at: {docx_file_path}")

if not LOCAL_FILE and os.path.exists(temp_audio_file_path):
    os.unlink(temp_audio_file_path)
    print("Temporary audio file deleted.")

print("Process finished successfully.")