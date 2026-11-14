"""Multi-agent summarization toolkit for MinerU.

Modules:
- prompts/: prompt templates for each agent
- ensemble_merge.py: deterministic merger for agent outputs
- scorer.py: quality metrics and report
- pipeline.py: CLI to orchestrate extract/merge/score
- reader.py: optional PDF/text extraction helpers
- utils.py: markdown parsing and helpers
"""

__all__ = [
    "__version__",
]

__version__ = "0.1.0"
