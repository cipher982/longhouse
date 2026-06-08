import { useEffect, useMemo, useState } from "react";
import { Button } from "../ui";
import { CheckCircleIcon, MessageSquareIcon, XIcon } from "../icons";
import type {
  PauseRequestResponseRequest,
  SessionPauseQuestion,
  SessionPauseQuestionOption,
  SessionPauseRequest,
} from "../../services/api/agents";

type AnswerValue = string | string[];
type AnswerDraft = Record<string, AnswerValue>;

interface SessionPauseRequestPanelProps {
  pauseRequest: SessionPauseRequest;
  onRespond: (body: PauseRequestResponseRequest) => Promise<void>;
}

function optionValue(option: SessionPauseQuestionOption): string {
  const raw = option.value ?? option.label;
  return String(raw ?? "").trim();
}

function questionKey(question: SessionPauseQuestion, index: number): string {
  const raw = String(question.id || "").trim();
  return raw || `question-${index + 1}`;
}

function answerHasValue(value: AnswerValue | undefined): boolean {
  if (Array.isArray(value)) return value.some((item) => item.trim().length > 0);
  return Boolean(value?.trim());
}

function answerParts(questions: SessionPauseQuestion[], draft: AnswerDraft): string[] {
  return questions.flatMap((question, index) => {
    const key = questionKey(question, index);
    const values = normalizedAnswerValues(draft[key]);
    if (values.length === 0) return [];
    const label = question.header || question.question;
    return [`${label}: ${values.join(", ")}`];
  });
}

function normalizedAnswerValues(value: AnswerValue | undefined): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => item.trim()).filter(Boolean);
  }
  const single = value?.trim();
  return single ? [single] : [];
}

function normalizedAnswers(questions: SessionPauseQuestion[], draft: AnswerDraft): Record<string, string[]> {
  return Object.fromEntries(
    questions.map((question, index) => {
      const key = questionKey(question, index);
      return [key, normalizedAnswerValues(draft[key])];
    }),
  );
}

function initialDraft(questions: SessionPauseQuestion[]): AnswerDraft {
  return Object.fromEntries(
    questions.map((question, index) => {
      const key = questionKey(question, index);
      return [key, question.multi_select ? [] : ""];
    }),
  );
}

export function SessionPauseRequestPanel({
  pauseRequest,
  onRespond,
}: SessionPauseRequestPanelProps) {
  const questions = useMemo(() => pauseRequest.questions ?? [], [pauseRequest.questions]);
  const [draft, setDraft] = useState<AnswerDraft>(() => initialDraft(questions));
  const [fallbackMessage, setFallbackMessage] = useState("");
  const [submitting, setSubmitting] = useState<"answer" | "reject" | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(initialDraft(questions));
    setFallbackMessage("");
    setError(null);
    setSubmitting(null);
    setSubmitted(false);
  }, [pauseRequest.id, questions]);

  const canAnswer =
    pauseRequest.can_respond &&
    !submitted &&
    (questions.length > 0
      ? questions.every((question, index) => answerHasValue(draft[questionKey(question, index)]))
      : fallbackMessage.trim().length > 0);

  const providerLabel = pauseRequest.provider
    ? pauseRequest.provider.slice(0, 1).toUpperCase() + pauseRequest.provider.slice(1)
    : "Provider";
  const detail =
    pauseRequest.summary?.trim() ||
    (pauseRequest.can_respond
      ? `${providerLabel} is waiting for your answer.`
      : "Answer this in the terminal or reconnect the host.");

  async function submitAnswer() {
    setSubmitting("answer");
    setError(null);
    try {
      const message =
        answerParts(questions, draft).join("; ") ||
        fallbackMessage.trim() ||
        undefined;
      const body: PauseRequestResponseRequest = {
        decision: "answer",
        message,
      };
      if (questions.length > 0) {
        body.answers = normalizedAnswers(questions, draft);
      } else {
        body.answers = null;
        body.content = fallbackMessage.trim();
      }
      await onRespond(body);
      setSubmitted(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to send answer");
    } finally {
      setSubmitting(null);
    }
  }

  async function rejectRequest() {
    setSubmitting("reject");
    setError(null);
    try {
      await onRespond({
        decision: "cancel",
        answers: null,
        content: null,
        message: "Cancelled in Longhouse.",
      });
      setSubmitted(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to cancel request");
    } finally {
      setSubmitting(null);
    }
  }

  function setQuestionAnswer(key: string, value: AnswerValue) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function toggleMultiValue(key: string, value: string, checked: boolean) {
    const current = draft[key];
    const values = Array.isArray(current) ? current : [];
    setQuestionAnswer(
      key,
      checked ? [...values, value] : values.filter((item) => item !== value),
    );
  }

  return (
    <section className="session-pause-panel" data-testid="session-pause-panel">
      <div className="session-pause-panel__header">
        <MessageSquareIcon width={16} height={16} />
        <div className="session-pause-panel__title-block">
          <span className="session-pause-panel__eyebrow">Needs answer</span>
          <h2>{pauseRequest.title?.trim() || "Provider question"}</h2>
          <p>{detail}</p>
        </div>
      </div>

      {questions.length > 0 ? (
        <div className="session-pause-panel__questions">
          {questions.map((question, index) => {
            const key = questionKey(question, index);
            const options = question.options ?? [];
            const currentValue = draft[key];
            return (
              <fieldset key={key} className="session-pause-question">
                <legend>
                  {question.header ? (
                    <span className="session-pause-question__header">{question.header}</span>
                  ) : null}
                  <span>{question.question}</span>
                </legend>
                {options.length > 0 ? (
                  <div className="session-pause-options">
                    {options.map((option, optionIndex) => {
                      const value = optionValue(option);
                      const inputId = `pause-${pauseRequest.id}-${key}-${optionIndex}`;
                      const checked = Array.isArray(currentValue)
                        ? currentValue.includes(value)
                        : currentValue === value;
                      return (
                        <label key={`${value}-${optionIndex}`} htmlFor={inputId} className="session-pause-option">
                          <input
                            id={inputId}
                            type={question.multi_select ? "checkbox" : "radio"}
                            name={`pause-${pauseRequest.id}-${key}`}
                            checked={checked}
                            disabled={!pauseRequest.can_respond || submitting != null || submitted}
                            onChange={(event) => {
                              if (question.multi_select) {
                                toggleMultiValue(key, value, event.currentTarget.checked);
                              } else {
                                setQuestionAnswer(key, value);
                              }
                            }}
                          />
                          <span className="session-pause-option__copy">
                            <span className="session-pause-option__label">{option.label}</span>
                            {option.description ? (
                              <span className="session-pause-option__description">{option.description}</span>
                            ) : null}
                          </span>
                        </label>
                      );
                    })}
                  </div>
                ) : (
                  <textarea
                    className="session-pause-freeform"
                    value={typeof currentValue === "string" ? currentValue : ""}
                    disabled={!pauseRequest.can_respond || submitting != null || submitted}
                    onChange={(event) => setQuestionAnswer(key, event.currentTarget.value)}
                    rows={2}
                  />
                )}
              </fieldset>
            );
          })}
        </div>
      ) : pauseRequest.can_respond ? (
        <textarea
          className="session-pause-freeform"
          value={fallbackMessage}
          disabled={submitting != null || submitted}
          onChange={(event) => setFallbackMessage(event.currentTarget.value)}
          rows={2}
          aria-label="Answer"
        />
      ) : null}

      {error ? <p className="session-pause-panel__error">{error}</p> : null}

      <div className="session-pause-panel__actions">
        {pauseRequest.can_respond ? (
          <>
            <Button
              type="button"
              variant="primary"
              size="sm"
              onClick={() => void submitAnswer()}
              disabled={!canAnswer || submitting != null}
            >
              <CheckCircleIcon width={14} height={14} />
              <span>{submitting === "answer" ? "Sending" : "Send answer"}</span>
            </Button>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => void rejectRequest()}
              disabled={submitting != null || submitted}
            >
              <XIcon width={14} height={14} />
              <span>{submitting === "reject" ? "Cancelling" : "Cancel"}</span>
            </Button>
          </>
        ) : (
          <span className="session-pause-panel__terminal-note">
            Waiting in terminal
          </span>
        )}
      </div>
    </section>
  );
}
