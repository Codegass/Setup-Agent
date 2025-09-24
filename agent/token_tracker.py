"""Token usage tracking system for ReAct Engine."""

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


class TokenTracker:
    """Tracks token usage for LLM calls during ReAct execution."""

    def __init__(self):
        self.token_records: List[Dict[str, Any]] = []
        self.current_iteration = 0

    def set_iteration(self, iteration: int):
        """Set the current iteration number."""
        self.current_iteration = iteration

    def track_token_usage(
        self,
        response: Any,
        model: str,
        step_type: str,
        tool_name: Optional[str] = None,
        iteration: Optional[int] = None
    ) -> None:
        """
        Extract and record token usage from LLM response.

        Args:
            response: LiteLLM response object
            model: Model name used for the request
            step_type: 'thought' or 'action'
            tool_name: Tool name for actions, 'Think' for thoughts
            iteration: Iteration number (uses current_iteration if not provided)
        """
        try:
            # Use provided iteration or current iteration
            iter_num = iteration if iteration is not None else self.current_iteration

            # Set default tool name based on step type
            if tool_name is None:
                tool_name = "Think" if step_type == "thought" else "Unknown"

            # Extract basic token usage
            total_tokens = 0
            prompt_tokens = 0
            completion_tokens = 0

            if hasattr(response, "usage") and response.usage:
                total_tokens = getattr(response.usage, "total_tokens", 0)
                prompt_tokens = getattr(response.usage, "prompt_tokens", 0)
                completion_tokens = getattr(response.usage, "completion_tokens", 0)

            # Extract reasoning tokens for supported models
            reasoning_tokens = 0
            if self.is_reasoning_model(model):
                reasoning_tokens = self._extract_reasoning_tokens(response)

            # Calculate actual output tokens
            actual_output_tokens = completion_tokens - reasoning_tokens

            # Create token record
            record = {
                'iteration': iter_num,
                'timestamp': datetime.now().isoformat(),
                'type': step_type,
                'tool_name': tool_name,
                'model': model,
                'total_tokens': total_tokens,
                'prompt_tokens': prompt_tokens,
                'completion_tokens': completion_tokens,
                'reasoning_tokens': reasoning_tokens,
                'actual_output_tokens': actual_output_tokens
            }

            self.token_records.append(record)

            # Log token usage in debug mode
            logger.debug(
                f"Token usage tracked - {step_type}:{tool_name} "
                f"Total:{total_tokens} Prompt:{prompt_tokens} "
                f"Reasoning:{reasoning_tokens} Output:{actual_output_tokens}"
            )

        except Exception as e:
            logger.warning(f"Failed to track token usage: {e}")

    def is_reasoning_model(self, model: str) -> bool:
        """
        Check if model supports reasoning tokens.

        Args:
            model: Model name

        Returns:
            True if model supports reasoning tokens
        """
        model_lower = model.lower()

        # Models that support reasoning tokens
        reasoning_patterns = [
            "gpt5", "gpt-5",          # GPT-5 variants
            "o1",                     # O1 models
            "o4", "o4-mini"          # O4 models
        ]

        return any(pattern in model_lower for pattern in reasoning_patterns)

    def _extract_reasoning_tokens(self, response: Any) -> int:
        """
        Extract reasoning tokens from response usage details.

        Args:
            response: LiteLLM response object

        Returns:
            Number of reasoning tokens, 0 if not available
        """
        try:
            # Check for reasoning tokens in completion_tokens_details
            if (hasattr(response, "usage") and
                response.usage and
                hasattr(response.usage, "completion_tokens_details") and
                response.usage.completion_tokens_details):

                # Get reasoning tokens attribute
                reasoning_tokens = getattr(
                    response.usage.completion_tokens_details,
                    'reasoning_tokens',
                    0
                )
                return reasoning_tokens if reasoning_tokens is not None else 0

        except Exception as e:
            logger.debug(f"Could not extract reasoning tokens: {e}")

        return 0

    def update_last_tool_name(self, tool_name: str):
        """
        Update the tool name of the last token record.

        This is useful when the tool name is determined after the LLM response.

        Args:
            tool_name: The actual tool name to set
        """
        if self.token_records:
            self.token_records[-1]['tool_name'] = tool_name
            logger.debug(f"Updated last token record tool name to: {tool_name}")

    def export_to_csv(self, filepath: str) -> bool:
        """
        Export token records to CSV file.

        Args:
            filepath: Path where to save the CSV file

        Returns:
            True if export successful, False otherwise
        """
        try:
            # Ensure directory exists
            Path(filepath).parent.mkdir(parents=True, exist_ok=True)

            if not self.token_records:
                logger.warning("No token records to export")
                return False

            # Define CSV columns
            fieldnames = [
                'iteration', 'timestamp', 'type', 'tool_name', 'model',
                'total_tokens', 'prompt_tokens', 'completion_tokens',
                'reasoning_tokens', 'actual_output_tokens'
            ]

            # Write CSV file
            with open(filepath, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(self.token_records)

            logger.info(
                f"Token usage exported to CSV: {filepath} "
                f"({len(self.token_records)} records)"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to export token usage to CSV: {e}")
            return False

    def get_summary_stats(self) -> Dict[str, Any]:
        """
        Get summary statistics of token usage.

        Returns:
            Dictionary with summary statistics
        """
        if not self.token_records:
            return {
                'total_records': 0,
                'total_tokens': 0,
                'total_prompt_tokens': 0,
                'total_reasoning_tokens': 0,
                'total_output_tokens': 0
            }

        # Calculate totals
        total_records = len(self.token_records)
        total_tokens = sum(record.get('total_tokens', 0) for record in self.token_records)
        total_prompt_tokens = sum(record.get('prompt_tokens', 0) for record in self.token_records)
        total_reasoning_tokens = sum(record.get('reasoning_tokens', 0) for record in self.token_records)
        total_output_tokens = sum(record.get('actual_output_tokens', 0) for record in self.token_records)

        # Count by type
        thoughts = sum(1 for record in self.token_records if record.get('type') == 'thought')
        actions = sum(1 for record in self.token_records if record.get('type') == 'action')

        # Count by model
        models = {}
        for record in self.token_records:
            model = record.get('model', 'unknown')
            models[model] = models.get(model, 0) + record.get('total_tokens', 0)

        # Count reasoning model usage
        reasoning_model_records = sum(
            1 for record in self.token_records
            if self.is_reasoning_model(record.get('model', ''))
        )

        return {
            'total_records': total_records,
            'total_tokens': total_tokens,
            'total_prompt_tokens': total_prompt_tokens,
            'total_reasoning_tokens': total_reasoning_tokens,
            'total_output_tokens': total_output_tokens,
            'thoughts_count': thoughts,
            'actions_count': actions,
            'reasoning_model_records': reasoning_model_records,
            'tokens_by_model': models,
            'average_tokens_per_call': total_tokens / total_records if total_records > 0 else 0
        }

    def log_summary(self):
        """Log a summary of token usage."""
        stats = self.get_summary_stats()

        logger.info("🔢 Token Usage Summary:")
        logger.info(f"  Total API calls: {stats['total_records']}")
        logger.info(f"  Total tokens: {stats['total_tokens']:,}")
        logger.info(f"  Prompt tokens: {stats['total_prompt_tokens']:,}")
        logger.info(f"  Reasoning tokens: {stats['total_reasoning_tokens']:,}")
        logger.info(f"  Output tokens: {stats['total_output_tokens']:,}")
        logger.info(f"  Thoughts: {stats['thoughts_count']}, Actions: {stats['actions_count']}")
        logger.info(f"  Reasoning model calls: {stats['reasoning_model_records']}")
        logger.info(f"  Average tokens per call: {stats['average_tokens_per_call']:.1f}")

        if stats['tokens_by_model']:
            logger.info("  Tokens by model:")
            for model, tokens in stats['tokens_by_model'].items():
                logger.info(f"    {model}: {tokens:,}")