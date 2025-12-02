"use client";

import React, { useState } from "react";
import "@copilotkit/react-ui/styles.css";
import { CopilotKit, useFrontendTool } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";

const Page: React.FC = () => {
  return (
    <CopilotKit
      // This should match your Next.js API route: src/app/api/copilotkit/route.ts
      runtimeUrl="/api/copilotkit"
      showDevConsole={false}
      agent="my_agent" // or "my_agent" – whatever you configured in your backend
    >
      <Chat />
    </CopilotKit>
  );
};

const Chat: React.FC = () => {
  // default to CopilotKit's background color via CSS variable
  const [background, setBackground] = useState<string>(
    "var(--copilot-kit-background-color)"
  );


  return (
    <div
      className="flex justify-center items-center h-full w-full"
      data-testid="background-container"
      style={{ background }}
    >
      <div className="h-full w-full md:w-8/10 md:h-8/10 rounded-lg">
        
        <CopilotChat
          className="h-full rounded-2xl max-w-6xl mx-auto"
          labels={{ initial: "Please enter a hospital name to find its CCN or a NPI to find the hospital type." }}
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
