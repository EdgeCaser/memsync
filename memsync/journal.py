
import json
import os
from datetime import datetime

def log_transaction(
    transaction_type: str,
    input_data: dict,
    memory_before: str,
    memory_after: str,
    llm_metadata: dict,
    journal_dir: str = "journal",
) -> None:
    """
    Logs a transaction to a structured JSON file in the journal directory.

    Args:
        transaction_type: The type of transaction (e.g., "refresh", "harvest").
        input_data: The data that initiated the transaction (e.g., notes, transcript path).
        memory_before: The content of the memory before the transaction.
        memory_after: The content of the memory after the transaction.
        llm_metadata: Metadata from the LLM call (e.g., token counts, model used, success status).
        journal_dir: The directory where journal entries will be stored.
    """
    os.makedirs(journal_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    transaction_id = f"{transaction_type}_{timestamp}"
    
    log_entry = {
        "transaction_id": transaction_id,
        "timestamp": datetime.now().isoformat(),
        "transaction_type": transaction_type,
        "input_data": input_data,
        "memory_before": memory_before,
        "memory_after": memory_after,
        "llm_metadata": llm_metadata,
    }

    file_path = os.path.join(journal_dir, f"{transaction_id}.json")
    with open(file_path, "w") as f:
        json.dump(log_entry, f, indent=2)

    print(f"Transaction logged to: {file_path}")

if __name__ == "__main__":
    # Example usage for testing
    print("Running example transaction logging...")
    
    example_input_refresh = {"notes": "Added a new thought about AI ethics."}
    example_memory_before = """Initial memory content.
"""
    example_memory_after = """Initial memory content.
Added a new thought about AI ethics.
"""
    example_llm_metadata_success = {
        "model": "gemini-pro",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "success": True,
        "changed": True,
        "truncated": False,
        "malformed": False,
    }
    
    log_transaction(
        transaction_type="refresh",
        input_data=example_input_refresh,
        memory_before=example_memory_before,
        memory_after=example_memory_after,
        llm_metadata=example_llm_metadata_success,
        journal_dir="example_journal" # Use a specific dir for example
    )

    example_input_harvest = {"transcript_path": "sessions/20230416_meeting.md"}
    example_memory_before_harvest = """Memory before harvest.
"""
    example_memory_after_harvest = """Memory before harvest.
Key points from meeting: AI ethics discussion.
"""
    example_llm_metadata_failure = {
        "model": "gemini-pro",
        "prompt_tokens": 120,
        "completion_tokens": 0,
        "success": False,
        "error": "API rate limit exceeded",
        "changed": False,
        "truncated": False,
        "malformed": False,
    }

    log_transaction(
        transaction_type="harvest",
        input_data=example_input_harvest,
        memory_before=example_memory_before_harvest,
        memory_after=example_memory_after_harvest,
        llm_metadata=example_llm_metadata_failure,
        journal_dir="example_journal"
    )
    print("Example logging complete.")
