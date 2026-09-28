import { useState } from "react";

interface FAQ {
  question: string;
  answer: string;
}

function buildFaqs(): FAQ[] {
  return [
    {
      question: "Is Longhouse another coding agent?",
      answer:
        "No. Claude Code, Codex, Cursor, OpenCode, and the other provider clients still run the agent loop. Longhouse gives their sessions a shared history and control surface.",
    },
    {
      question: "Which sessions can I control?",
      answer:
        "Sessions started outside Longhouse are searchable and inspectable. Sessions started through Longhouse can also be controlled: send the next instruction, interrupt a turn, steer it, and resume where the provider supports it. The provider list above shows what is currently proven for each CLI.",
    },
    {
      question: "What happens when my laptop sleeps?",
      answer:
        "Work running on that laptop pauses or disconnects until the laptop wakes. Put the Longhouse server on a Mac mini, home server, VPS, or hosted account to keep the timeline and web UI available while the laptop is offline.",
    },
    {
      question: "Does Longhouse move the work to its own cloud?",
      answer:
        "No. The provider client keeps running on the machine you selected. The web UI shows the session and sends control requests back to that machine.",
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
  const faqs = buildFaqs();

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
