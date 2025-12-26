import { useState } from "react";
import { Link } from "react-router-dom";
import { LockIcon, ShieldIcon, TrashIcon, BanIcon } from "../icons";

interface FAQ {
  question: string;
  answer: string;
}

const faqs: FAQ[] = [
  {
    question: "How does authentication work?",
    answer: "We use Google OAuth for secure sign-in. Your credentials are never stored on our servers — we use industry-standard JWT tokens for session management."
  },
  {
    question: "Where is my data stored?",
    answer: "Your data is stored in a PostgreSQL database. We never sell or share your personal information with third parties."
  },
  {
    question: "Can I delete my data?",
    answer: "Yes! Full account deletion is available in your profile settings. When you delete your account, all your data, agents, and workflows are permanently removed."
  },
  {
    question: "Do you train AI models on my data?",
    answer: "No. Your conversations and data are never used to train any AI models. Your data is yours alone."
  },
  {
    question: "What LLM do you use?",
    answer: "Swarmlet currently uses OpenAI models (default: GPT-5.2). You can pick from the available models depending on your quality/cost needs."
  }
];

export function TrustSection() {
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  const toggleFAQ = (index: number) => {
    setOpenIndex(openIndex === index ? null : index);
  };

  return (
    <section className="landing-trust">
      <div className="landing-section-inner">
        <h2 className="landing-section-title">Questions? We've Got Answers.</h2>
        <p className="landing-section-subtitle">
          Built with privacy and security in mind.
        </p>

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

        {/* Trust badges - linked to Security page */}
        <Link to="/security" className="landing-trust-badges-link">
          <div className="landing-trust-badges">
            <div className="landing-trust-badge">
              <LockIcon width={18} height={18} className="landing-trust-icon-svg" />
              <span>Credentials encrypted</span>
            </div>
            <div className="landing-trust-badge">
              <ShieldIcon width={18} height={18} className="landing-trust-icon-svg" />
              <span>HTTPS everywhere</span>
            </div>
            <div className="landing-trust-badge">
              <TrashIcon width={18} height={18} className="landing-trust-icon-svg" />
              <span>Full data deletion</span>
            </div>
            <div className="landing-trust-badge">
              <BanIcon width={18} height={18} className="landing-trust-icon-svg" />
              <span>No training on your data</span>
            </div>
          </div>
          <p className="landing-trust-link-text">Learn more about our security practices →</p>
        </Link>
      </div>
    </section>
  );
}
