import { render, screen } from "@testing-library/react";

function Wordmark() {
  return (
    <span>
      Bot<span>AI</span>
    </span>
  );
}

test("renders wordmark", () => {
  render(<Wordmark />);
  expect(screen.getByText("AI")).toBeInTheDocument();
});
