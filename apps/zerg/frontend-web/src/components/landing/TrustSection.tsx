import { useState } from "react";

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
    answer: "Your data is encrypted at rest and stored in a PostgreSQL database. We never sell or share your personal information with third parties."
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
    question: "What LLMs do you support?",
    answer: "We support OpenAI (GPT-4, GPT-3.5), Anthropic (Claude), Google (Gemini), and more. You can bring your own API keys for full control over costs."
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

        {/* Trust badges */}
        <div className="landing-trust-badges">
          <div className="landing-trust-badge">
            <span className="landing-trust-icon">🔒</span>
            <span>Encrypted at rest</span>
          </div>
          <div className="landing-trust-badge">
            <span className="landing-trust-icon">🛡️</span>
            <span>SOC 2 roadmap</span>
          </div>
          <div className="landing-trust-badge">
            <span className="landing-trust-icon">🗑️</span>
            <span>Full data deletion</span>
          </div>
          <div className="landing-trust-badge">
            <span className="landing-trust-icon">🚫</span>
            <span>No training on your data</span>
          </div>
        </div>
      </div>
    </section>
  );
}

