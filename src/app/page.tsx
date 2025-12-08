"use client";

import React, { useState } from "react";
import "@copilotkit/react-ui/styles.css";
import { CopilotKit } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";

const Page: React.FC = () => {
  return (
    <CopilotKit
      runtimeUrl={process.env.NEXT_PUBLIC_API_BASE_URL} // <-- connects to https://a.onrender.com
      showDevConsole={false}
      agent="my_agent"
    >
      <Chat />
    </CopilotKit>
  );
};

const Chat: React.FC = () => {
  const [background] = useState<string>(
    "var(--copilot-kit-background-color)"
  );

  return (
    <div
      className="flex justify-center items-center h-full w-full"
      data-testid="background-container"
      style={{ background }}
    >
      <div className="h-full w-full md:w-8/10 md:h-8/10 rounded-lg">
        <div className="text-sm font-semibold mb-2 ml-2 text-gray-400">
          Sample suggestions:
        </div>

        <CopilotChat
          className="h-full rounded-2xl max-w-6xl mx-auto"
          labels={{
            initial:
              "Please enter a hospital name to find its CCN or a NPI to find the hospital type.",
          }}
          suggestions={[
            {
              title: "Find CCN for Bayonne Medical Center",
              message: "Find CCN for Bayonne Medical Center",
            },
            {
              title: "Find hospital type for 1104144641",
              message: "Find hospital type for 1104144641",
            },
          ]}
        />
      </div>
    </div>
  );
};

export default Page;
