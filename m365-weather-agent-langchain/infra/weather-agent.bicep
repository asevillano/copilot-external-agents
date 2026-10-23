targetScope = 'resourceGroup'

// LangChain weather agent exposed through MCP. External ingress because the bridge runs in
// another ACA environment; access requires an Entra app token with the Agent.Invoke role.
param location string = 'eastus'
param appName string = 'ca-weather-agent-mcp'
param environmentId string
param defaultDomain string
param registryServer string
param image string
param identityId string
param identityClientId string
param tenantId string
param mcpApiAppId string
param allowedClientIds string
param openAiEndpoint string
param openAiDeployment string = 'gpt-5.6-luna'
@minValue(0)
@maxValue(1)
param minReplicas int = 1

var host = '${appName}.${defaultDomain}'

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  tags: { purpose: 'm365-weather-agent-langchain', component: 'weather-agent-mcp' }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
      registries: [ { server: registryServer, identity: identityId } ]
    }
    template: {
      containers: [
        {
          name: 'agent'
          image: image
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: [
            { type: 'Startup', httpGet: { path: '/healthz', port: 8080, scheme: 'HTTP' }, periodSeconds: 5, failureThreshold: 30 }
            { type: 'Readiness', httpGet: { path: '/healthz', port: 8080, scheme: 'HTTP' }, periodSeconds: 10 }
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8080, scheme: 'HTTP' }, periodSeconds: 30, failureThreshold: 3 }
          ]
          env: [
            { name: 'PORT', value: '8080' }
            { name: 'HOST', value: '0.0.0.0' }
            { name: 'AZURE_CLIENT_ID', value: identityClientId }
            { name: 'AZURE_OPENAI_ENDPOINT', value: openAiEndpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: openAiDeployment }
            { name: 'AZURE_OPENAI_API_VERSION', value: '2025-04-01-preview' }
            { name: 'ENTRA_TENANT_ID', value: tenantId }
            { name: 'MCP_AUDIENCES', value: 'api://${mcpApiAppId},${mcpApiAppId}' }
            { name: 'MCP_ALLOWED_CLIENT_IDS', value: allowedClientIds }
            { name: 'MCP_RESOURCE_URL', value: 'https://${host}/mcp' }
            { name: 'MCP_ALLOWED_HOSTS', value: '${host},${host}:*' }
          ]
        }
      ]
      // conversation memory (InMemorySaver) is per process
      scale: {
        minReplicas: minReplicas
        maxReplicas: 1
        rules: [ { name: 'http', http: { metadata: { concurrentRequests: '10' } } } ]
      }
    }
  }
}

output mcpUrl string = 'https://${host}/mcp'
output resourceId string = app.id
