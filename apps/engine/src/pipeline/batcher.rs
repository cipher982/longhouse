use std::ops::Range;

use anyhow::{bail, Result};

use super::parser::{ParsedEvent, ParsedSourceLine};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShipRange {
    pub start_offset: u64,
    pub end_offset: u64,
    pub source_line_range: Range<usize>,
    pub event_range: Range<usize>,
}

impl ShipRange {
    pub fn byte_len(&self) -> u64 {
        self.end_offset.saturating_sub(self.start_offset)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeadLetterRange {
    pub start_offset: u64,
    pub end_offset: u64,
    pub source_line_range: Range<usize>,
    pub event_range: Range<usize>,
    pub byte_len: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PlannedRangeAction {
    Ship(ShipRange),
    DeadLetter(DeadLetterRange),
}

fn event_index_at_or_after(events: &[ParsedEvent], offset: u64) -> usize {
    events.partition_point(|event| event.source_offset < offset)
}

pub fn plan_range_actions(
    source_lines: &[ParsedSourceLine],
    events: &[ParsedEvent],
    start_offset: u64,
    end_offset: u64,
    max_batch_bytes: u64,
) -> Result<Vec<PlannedRangeAction>> {
    if max_batch_bytes == 0 {
        bail!("max_batch_bytes must be > 0");
    }
    if end_offset < start_offset {
        bail!(
            "range end {} is before range start {}",
            end_offset,
            start_offset
        );
    }
    if end_offset == start_offset {
        return Ok(Vec::new());
    }

    let start_line_idx = source_lines.partition_point(|line| line.source_offset < start_offset);
    let end_line_idx = source_lines.partition_point(|line| line.source_offset < end_offset);

    if start_line_idx == end_line_idx {
        return Ok(Vec::new());
    }

    if source_lines[start_line_idx].source_offset != start_offset {
        bail!(
            "range start {} is not aligned to a source-line boundary (next line starts at {})",
            start_offset,
            source_lines[start_line_idx].source_offset
        );
    }

    let mut actions = Vec::new();
    let mut batch_start_idx = start_line_idx;
    let mut batch_start_offset = start_offset;
    let mut batch_event_start = event_index_at_or_after(events, start_offset);

    for line_idx in start_line_idx..end_line_idx {
        let line_start = source_lines[line_idx].source_offset;
        let line_end = if line_idx + 1 < end_line_idx {
            source_lines[line_idx + 1].source_offset
        } else {
            end_offset
        };
        if line_end < line_start {
            bail!(
                "source lines are not ordered: line at {} ends before it starts",
                line_start
            );
        }

        let line_bytes = line_end - line_start;
        let line_event_start = event_index_at_or_after(events, line_start);
        let line_event_end = event_index_at_or_after(events, line_end);

        if line_bytes > max_batch_bytes {
            if batch_start_idx < line_idx {
                actions.push(PlannedRangeAction::Ship(ShipRange {
                    start_offset: batch_start_offset,
                    end_offset: line_start,
                    source_line_range: batch_start_idx..line_idx,
                    event_range: batch_event_start..line_event_start,
                }));
            }

            actions.push(PlannedRangeAction::DeadLetter(DeadLetterRange {
                start_offset: line_start,
                end_offset: line_end,
                source_line_range: line_idx..line_idx + 1,
                event_range: line_event_start..line_event_end,
                byte_len: line_bytes,
            }));

            batch_start_idx = line_idx + 1;
            batch_start_offset = line_end;
            batch_event_start = line_event_end;
            continue;
        }

        let proposed_bytes = line_end - batch_start_offset;
        if batch_start_idx < line_idx && proposed_bytes > max_batch_bytes {
            actions.push(PlannedRangeAction::Ship(ShipRange {
                start_offset: batch_start_offset,
                end_offset: line_start,
                source_line_range: batch_start_idx..line_idx,
                event_range: batch_event_start..line_event_start,
            }));
            batch_start_idx = line_idx;
            batch_start_offset = line_start;
            batch_event_start = line_event_start;
        }
    }

    if batch_start_idx < end_line_idx {
        actions.push(PlannedRangeAction::Ship(ShipRange {
            start_offset: batch_start_offset,
            end_offset,
            source_line_range: batch_start_idx..end_line_idx,
            event_range: batch_event_start..event_index_at_or_after(events, end_offset),
        }));
    }

    Ok(actions)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pipeline::parser::{ParsedEvent, ParsedSourceLine, Role};
    use chrono::Utc;

    fn line(offset: u64, raw_line: &str) -> ParsedSourceLine {
        ParsedSourceLine {
            source_offset: offset,
            raw_line: raw_line.to_string(),
        }
    }

    fn event(offset: u64, text: &str) -> ParsedEvent {
        ParsedEvent {
            uuid: format!("event-{}", offset),
            session_id: "session-1".to_string(),
            timestamp: Utc::now(),
            role: Role::User,
            content_text: Some(text.to_string()),
            tool_name: None,
            tool_input_json: None,
            tool_output_text: None,
            tool_call_id: None,
            source_offset: offset,
            raw_type: "user".to_string(),
            raw_line: Some(text.to_string()),
        }
    }

    fn covered_ranges(actions: &[PlannedRangeAction]) -> Vec<(u64, u64)> {
        actions
            .iter()
            .map(|action| match action {
                PlannedRangeAction::Ship(range) => (range.start_offset, range.end_offset),
                PlannedRangeAction::DeadLetter(range) => (range.start_offset, range.end_offset),
            })
            .collect()
    }

    #[test]
    fn test_plan_range_actions_splits_contiguously_under_limit() {
        let source_lines = vec![line(0, "a"), line(100, "b"), line(220, "c"), line(330, "d")];
        let events = vec![
            event(0, "a"),
            event(100, "b"),
            event(220, "c"),
            event(330, "d"),
        ];

        let actions = plan_range_actions(&source_lines, &events, 0, 430, 250).unwrap();

        assert_eq!(
            actions,
            vec![
                PlannedRangeAction::Ship(ShipRange {
                    start_offset: 0,
                    end_offset: 220,
                    source_line_range: 0..2,
                    event_range: 0..2,
                }),
                PlannedRangeAction::Ship(ShipRange {
                    start_offset: 220,
                    end_offset: 430,
                    source_line_range: 2..4,
                    event_range: 2..4,
                }),
            ]
        );
        assert_eq!(covered_ranges(&actions), vec![(0, 220), (220, 430)]);
    }

    #[test]
    fn test_plan_range_actions_caps_at_end_offset() {
        let source_lines = vec![line(0, "a"), line(100, "b"), line(220, "c"), line(330, "d")];
        let events = vec![
            event(0, "a"),
            event(100, "b"),
            event(220, "c"),
            event(330, "d"),
        ];

        let actions = plan_range_actions(&source_lines, &events, 100, 300, 500).unwrap();

        assert_eq!(
            actions,
            vec![PlannedRangeAction::Ship(ShipRange {
                start_offset: 100,
                end_offset: 300,
                source_line_range: 1..3,
                event_range: 1..3,
            })]
        );
    }

    #[test]
    fn test_plan_range_actions_dead_letters_single_oversize_line_and_continues() {
        let source_lines = vec![
            line(0, "meta"),
            line(700, "huge"),
            line(760, "tail-1"),
            line(830, "tail-2"),
        ];
        let events = vec![
            event(0, "meta"),
            event(700, "huge"),
            event(760, "tail-1"),
            event(830, "tail-2"),
        ];

        let actions = plan_range_actions(&source_lines, &events, 0, 900, 200).unwrap();

        assert_eq!(
            actions,
            vec![
                PlannedRangeAction::DeadLetter(DeadLetterRange {
                    start_offset: 0,
                    end_offset: 700,
                    source_line_range: 0..1,
                    event_range: 0..1,
                    byte_len: 700,
                }),
                PlannedRangeAction::Ship(ShipRange {
                    start_offset: 700,
                    end_offset: 900,
                    source_line_range: 1..4,
                    event_range: 1..4,
                }),
            ]
        );
        assert_eq!(covered_ranges(&actions), vec![(0, 700), (700, 900)]);
    }

    #[test]
    fn test_plan_range_actions_keeps_multiple_events_from_same_source_line_together() {
        let source_lines = vec![line(0, "tool-call"), line(120, "next")];
        let events = vec![
            event(0, "tool-call"),
            event(0, "tool-result"),
            event(120, "next"),
        ];

        let actions = plan_range_actions(&source_lines, &events, 0, 180, 1000).unwrap();
        let PlannedRangeAction::Ship(range) = &actions[0] else {
            panic!("expected ship range");
        };

        assert_eq!(range.event_range, 0..3);
    }
}
