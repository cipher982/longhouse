import js from "@eslint/js";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import tsParser from "@typescript-eslint/parser";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import prettier from "eslint-config-prettier";
import globals from "globals";

export default [
  {
    ignores: ["dist/**", "node_modules/**", "public/**", "src/generated/**"],
  },
  {
    ...js.configs.recommended,
    languageOptions: {
      ...js.configs.recommended.languageOptions,
      globals: (() => {
        const trimKeys = (source) =>
          Object.fromEntries(
            Object.entries(source).map(([key, value]) => [key.trim(), value])
          );
        return {
          ...trimKeys(globals.browser),
          ...trimKeys(globals.node),
        };
      })(),
      ecmaVersion: "latest",
      sourceType: "module",
    },
  },
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
      },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...tsPlugin.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/no-unused-vars": [
        "warn",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
        },
      ],
      "no-undef": "off",
      // Disabled: too noisy for co-located hooks/contexts, doesn't affect prod
      "react-refresh/only-export-components": "off",
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/hooks/usePageMeta.ts", "src/lib/readiness-contract.ts"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "AssignmentExpression[left.type='MemberExpression'][left.object.name='document'][left.property.name='title']",
          message: "Use usePageMeta() instead of assigning document.title directly.",
        },
        {
          selector:
            "CallExpression[callee.type='MemberExpression'][callee.object.name='document'][callee.property.name='querySelector'] Literal[value='meta[name=\"description\"]']",
          message: "Use usePageMeta() instead of mutating the meta description directly.",
        },
        {
          selector:
            "CallExpression[callee.type='MemberExpression'][callee.property.name='setAttribute'] Literal[value='data-ready']",
          message: "Use useReadinessFlag() instead of mutating data-ready directly.",
        },
        {
          selector:
            "CallExpression[callee.type='MemberExpression'][callee.property.name='removeAttribute'] Literal[value='data-ready']",
          message: "Use useReadinessFlag() instead of mutating data-ready directly.",
        },
        {
          selector:
            "CallExpression[callee.type='MemberExpression'][callee.property.name='setAttribute'] Literal[value='data-screenshot-ready']",
          message: "Use useReadinessFlag() instead of mutating data-screenshot-ready directly.",
        },
        {
          selector:
            "CallExpression[callee.type='MemberExpression'][callee.property.name='removeAttribute'] Literal[value='data-screenshot-ready']",
          message: "Use useReadinessFlag() instead of mutating data-screenshot-ready directly.",
        },
      ],
    },
  },
  {
    files: ["src/pages/*Page.tsx", "src/legacy/**/*Page.tsx"],
    ignores: [
      "src/pages/ChatPage.tsx",
      "src/pages/LandingPage.tsx",
      "src/pages/OikosChatPage.tsx",
      "src/pages/SessionsPage.tsx",
      "src/pages/SwarmOpsPage.tsx",
    ],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "react",
              importNames: ["useEffect"],
              message:
                "Page-level useEffect is restricted. Prefer route/query ownership or a named browser-sync hook. If the effect is truly necessary, add an explicit allowlist entry in eslint.config.js.",
            },
          ],
        },
      ],
      "no-restricted-syntax": [
        "error",
        {
          selector: "CallExpression[callee.name='useEffect']",
          message:
            "Page-level useEffect is restricted. Prefer route/query ownership or a named browser-sync hook. If the effect is truly necessary, add an explicit allowlist entry in eslint.config.js.",
        },
        {
          selector: "CallExpression[callee.object.name='React'][callee.property.name='useEffect']",
          message:
            "Page-level useEffect is restricted. Prefer route/query ownership or a named browser-sync hook. If the effect is truly necessary, add an explicit allowlist entry in eslint.config.js.",
        },
      ],
    },
  },
  {
    files: ["**/*.test.ts", "**/*.test.tsx", "src/test/**/*.ts"],
    rules: {
      "no-prototype-builtins": "off",
    },
  },
  prettier,
];
