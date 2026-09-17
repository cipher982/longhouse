import { useState } from "react";
import { certifiedChips, useProviderCertification, type ProviderCertificationPayload } from "../../lib/providerCertification";
import { getLaunchProviderSupportList } from "../../lib/providers";

interface FAQ {
  question: string;
  answer: string;
}

function strongestProvidersAnswer(certification: ProviderCertificationPayload | null): string {
  // Derived from the same certified chips as the provider list, never hand-written.
  const full = getLaunchProviderSupportList()
    .filter(({ id, proven }) => {
      const c = certifiedChips(id, proven, certification);
      return c.launchAndSend && c.interrupt && c.steerMidTurn && c.resume;
    })
    .map((provider) => provider.marketingName);
  const lead =
    full.length === 0
      ? "No provider has every control capability release-proven yet."
      : `${full.join(", ")} ${full.length === 1 ? "has" : "have"} launch, send, interrupt, mid-turn steering, and resume all release-proven.`;
  return `${lead} Each chip in the provider list above lights only while the provider factory has a current passing live test against the real binary.`;
}

function buildFaqs(certification: ProviderCertificationPayload | null): FAQ[] {
  return [
  {
    question: "Is Longhouse another coding agent?",
    answer:
      "No. Claude Code, Codex, Cursor, OpenCode, and the other provider clients still run the agent loop. Longhouse gives their sessions a shared history and control surface.",
  },
  {
    question: "Which sessions can I control?",
    answer:
      "Sessions started outside Longhouse are searchable and inspectable. Sessions started through Longhouse can also be controlled: send the next instruction, interrupt a turn, steer it, and resume where the provider supports it. The provider list above shows which of those the provider factory has proven for each CLI.",
  },
  {
    question: "What happens when my laptop sleeps?",
    answer:
      "Work running on that laptop pauses or disconnects until the laptop wakes. Put the Longhouse server on a Mac mini, home server, VPS, or hosted account to keep the timeline and web UI available while the laptop is offline.",
  },
  {
    question: "Does Longhouse move the work to my phone or to its own cloud?",
    answer:
      "No. The provider client keeps running on the machine you selected. The web and iPhone apps show the session and send control requests back to that machine.",
  },
  {
    question: "Which providers are strongest today?",
    answer: strongestProvidersAnswer(certification),
  },
  {
    question: "Where is my data stored?",
    answer:
      "A self-hosted archive lives in SQLite on the server you choose. With hosted, the archive lives on the private Longhouse server we operate for you.",
  },
];
}

export function TrustSection() {
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const faqs = buildFaqs(useProviderCertification());

  const toggleFAQ = (index: number) => {
    setOpenIndex(openIndex === index ? null : index);
  };

  return (
    <section className="landing-trust">
      <div className="landing-section-inner">
        <h2 className="landing-faq-heading">Common questions</h2>

        <div className="landing-faq-list">
          {faqs.map((faq, index) => (
            <div
              key={index}
              className={`landing-faq-item ${openIndex === index ? "open" : ""}`}
            >
              <button
                className="landing-faq-question"
                onClick={() => toggleFAQ(index)}
                aria-expanded={openIndex === index}
              >
                <span>{faq.question}</span>
                <span className="landing-faq-toggle">
                  {openIndex === index ? "−" : "+"}
                </span>
              </button>
              <div className="landing-faq-answer">
                <p>{faq.answer}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
