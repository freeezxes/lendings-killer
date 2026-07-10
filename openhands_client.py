import asyncio
import logging
import os

logger = logging.getLogger(__name__)

async def run_openhands_task(slug: str, prompt: str, timeout_seconds: int = 1800) -> bool:
    """
    Runs an OpenHands task for the given workspace slug via docker exec asynchronously.
    Assumes a docker container named 'lendings_openhands' is running.
    Returns True if successful, False otherwise.
    """
    workspace_dir = f"/opt/workspace_base/{slug}"
    
    command = [
        "docker", "exec", "-i", "lendings_openhands",
        "python", "-m", "openhands.core.main",
        "-t", prompt,
        "-d", workspace_dir
    ]
    
    logger.info(f"Starting OpenHands task for {slug}...")
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            
            if process.returncode != 0:
                logger.error(f"OpenHands task failed for {slug}. Return code: {process.returncode}\n{stderr.decode()}\n{stdout.decode()}")
                return False
                
            logger.info(f"OpenHands task completed for {slug}.")
            return True
            
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.error(f"OpenHands task timed out after {timeout_seconds}s for {slug}.")
            return False
            
    except Exception as e:
        logger.error(f"Failed to run OpenHands command for {slug}: {e}")
        return False
