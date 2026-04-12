import { useState } from "react";

interface FAQ {
  question: string;
  answer: string;
}

const faqs: FAQ[] = [
  {
    question: "Do I need a credit card to try it?",
    answer: "No. Self-hosting is free and open source. Hosted beta is optional convenience if you want us to run the server for you."
  },
  {
    question: "Where is my data stored?",
    answer: "When you self-host, everything lives in a SQLite database on your machine. Hosted beta runs your own isolated runtime. We never sell or share your data."
  },
  {
    question: "Why not just use ssh + tmux?",
    answer: "If you only need one remote shell, ssh + tmux is simpler. Longhouse is for when you want sessions to be searchable, addressable, and controllable from browser, CLI, or API."
  },
  {
    question: "Do you train AI models on my data?",
    answer: "No. Your sessions and data are never used to train any AI models."
  },
  {
    question: "Can I migrate from self-hosted to hosted?",
    answer: "Yes. Export your SQLite database and import it into your hosted instance. Same data, same sessions."
  },
];

export function TrustSection() {
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  const toggleFAQ = (index: number) => {
    setOpenIndex(openIndex === index ? null : index);
  };

  return (
    <section className="landing-trust">
      <div className="landing-section-inner">
        <p className="landing-section-label">FAQ</p>
        <h2 className="landing-section-title">Common questions.</h2>

        <div className="landing-faq-list">
          {faqs.map((faq, index) => (
            <div
              key={index}
              className={`landing-faq-item ${openIndex === index ? 'open' : ''}`}
            >
              <button
                className="landing-faq-question"
                onClick={() => toggleFAQ(index)}
                aria-expanded={openIndex === index}
              >
                <span>{faq.question}</span>
                <span className="landing-faq-toggle">
                  {openIndex === index ? '−' : '+'}
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
