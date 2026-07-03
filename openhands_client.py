import subprocess
import logging
import os

logger = logging.getLogger(__name__)

def run_openhands_task(slug: str, prompt: str, timeout_seconds: int = 600) -> bool:
    """
    Runs an OpenHands task for the given workspace slug via docker exec.
    Assumes a docker container named 'lendings_openhands' is running.
    Returns True if successful, False otherwise.
    """
    # The path inside the OpenHands container where the generated_sites are mounted
    workspace_dir = f"/opt/workspace_base/{slug}"
    
    # Use docker exec to run a headless task inside the openhands container
    # openhands.core.main is the entrypoint for headless operation
    command = [
        "docker", "exec", "-i", "lendings_openhands",
        "python", "-m", "openhands.core.main",
        "-t", prompt,
        "-d", workspace_dir
    ]
    
    logger.info(f"Starting OpenHands task for {slug}...")
    try:
        # We set a large timeout because building a React app takes time
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds)
        
        # OpenHands might write to stderr even on success, so we rely on returncode
        if result.returncode != 0:
            logger.error(f"OpenHands task failed for {slug}. Return code: {result.returncode}\n{result.stderr}\n{result.stdout}")
            return False
            
        logger.info(f"OpenHands task completed for {slug}.")
        return True
    except subprocess.TimeoutExpired:
        logger.error(f"OpenHands task timed out after {timeout_seconds}s for {slug}.")
        return False
    except Exception as e:
        logger.error(f"Failed to run OpenHands command for {slug}: {e}")
        return False
