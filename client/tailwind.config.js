/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Bench-wood palette: dark walnut bench, spruce accent, brass hardware.
        void: "#0b0a09",
        bench: {
          900: "#141210",
          800: "#1c1917",
          700: "#272320",
          600: "#332e29",
          500: "#4a423b",
        },
        spruce: "#2f6f5e",
        brass: "#c8963e",
        rosin: "#e0c68a",
        varnish: "#f4ead6",
        crack: "#c04a3f",
        warp: "#b8862f",
        cured: "#4f8a5b",
      },
      fontFamily: {
        display: ["Georgia", "Times New Roman", "serif"],
        mono: [
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Monaco",
          "Cascadia Mono",
          "monospace",
        ],
      },
      boxShadow: {
        inlay: "inset 0 1px 0 0 rgba(244,234,214,0.05)",
      },
    },
  },
  plugins: [],
};
