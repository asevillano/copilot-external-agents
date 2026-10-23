from vertexai.agent_engines import AdkApp

from renewal_risk_agent.agent import root_agent

app = AdkApp(agent=root_agent)
