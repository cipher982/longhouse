import React from "react";

/**
 * Code-drawn device chrome so REAL captured screenshots (from the web + iOS
 * capture pipelines) can be dropped in without a baked-in mockup asset. We
 * draw only the chrome the screenshot can't show; the screenshot is the truth.
 */

interface FrameProps {
  src: string;
  children?: React.ReactNode;
}

/** A clean dark browser window wrapping a web screenshot. */
export const BrowserFrame: React.FC<FrameProps> = ({ src, children }) => {
  return (
    <div
      style={{
        width: "76%",
        borderRadius: 14,
        overflow: "hidden",
        boxShadow: "0 40px 120px rgba(0,0,0,0.55)",
        border: "1px solid rgba(255,255,255,0.08)",
        background: "#15151c",
      }}
    >
      {/* title bar */}
      <div
        style={{
          height: 40,
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "0 16px",
          background: "#1d1d27",
          borderBottom: "1px solid rgba(255,255,255,0.06)",
        }}
      >
        {["#ff5f57", "#febc2e", "#28c840"].map((c) => (
          <span
            key={c}
            style={{ width: 12, height: 12, borderRadius: "50%", background: c }}
          />
        ))}
        <div
          style={{
            marginLeft: 16,
            flex: 1,
            maxWidth: 360,
            height: 22,
            borderRadius: 11,
            background: "rgba(255,255,255,0.06)",
            display: "flex",
            alignItems: "center",
            paddingLeft: 14,
            fontSize: 13,
            color: "rgba(255,255,255,0.45)",
            fontFamily: "-apple-system, BlinkMacSystemFont, sans-serif",
          }}
        >
          longhouse.local
        </div>
      </div>
      <div style={{ position: "relative" }}>
        <img src={src} style={{ width: "100%", display: "block" }} />
        {children}
      </div>
    </div>
  );
};

/** A phone bezel wrapping a portrait iOS screenshot. */
export const PhoneFrame: React.FC<FrameProps> = ({ src, children }) => {
  return (
    <div
      style={{
        height: "82%",
        aspectRatio: "1206 / 2622",
        borderRadius: 52,
        padding: 12,
        background: "#0c0c10",
        border: "2px solid rgba(255,255,255,0.12)",
        boxShadow: "0 40px 120px rgba(0,0,0,0.6)",
        position: "relative",
      }}
    >
      <div
        style={{
          width: "100%",
          height: "100%",
          borderRadius: 42,
          overflow: "hidden",
          position: "relative",
          background: "#000",
        }}
      >
        <img
          src={src}
          style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
        />
        {children}
      </div>
    </div>
  );
};
