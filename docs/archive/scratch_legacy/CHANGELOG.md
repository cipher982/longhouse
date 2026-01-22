# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Token usage and cost tracking for agent runs
  - Automatically captures token counts and costs from OpenAI API calls
  - Persists metrics to AgentRun database records
  - Displays metrics in Run History accordion in Dashboard
  - Real-time WebSocket updates for token/cost metrics

### Changed

### Fixed

## [0.9.0] - Initial Release

### Added
- Agent Dashboard with real-time status updates
- Chat interface for interacting with agents
- Task scheduling with cron expressions
- LangGraph-based agent definitions
- Run History accordion showing execution details
- SQLite database for persistence
