targetScope = 'resourceGroup'

// -mcp bridge: public ingress only for Bot Service. No Google Cloud configuration at all;
// it reaches the agent through the internal MCP adapter with an Entra app token.
param location string = 'eastus'
param appName string = 'ca-renewal-risk-m365-mcp'
param environmentId string
param registryServer string
param image string
param identityId string
param identityClientId string
@secure()
param botClientSecret string
param botClientId string
param botTenantId string
param allowedUserIds string
param oauthConnectionName string
param mcpServerUrl string
param mcpApiAppId string
@minValue(0)
@maxValue(1)
param minReplicas int = 1

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: appName
  location: location
  tags: { purpose: 'm365-gcp-bridge-mcp', component: 'bridge' }
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
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      secrets: [
        { name: 'bot-client-secret', value: botClientSecret }
      ]
      registries: [
        { server: registryServer, identity: identityId }
      ]
    }
    template: {
      containers: [
        {
          name: 'bridge'
          image: image
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: [
            { type: 'Startup', httpGet: { path: '/healthz', port: 8000, scheme: 'HTTP' }, periodSeconds: 5, failureThreshold: 30 }
            { type: 'Readiness', httpGet: { path: '/healthz', port: 8000, scheme: 'HTTP' }, periodSeconds: 10 }
            { type: 'Liveness', httpGet: { path: '/healthz', port: 8000, scheme: 'HTTP' }, periodSeconds: 30, failureThreshold: 3 }
          ]
          env: [
            { name: 'LOCAL_DEVELOPMENT', value: 'false' }
            { name: 'AGENT_TIMEOUT_SECONDS', value: '300' }
            { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID', value: botClientId }
            { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID', value: botTenantId }
            { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET', secretRef: 'bot-client-secret' }
            { name: 'M365_TENANT_ID', value: botTenantId }
            { name: 'ENTRA_OAUTH_CONNECTION_NAME', value: oauthConnectionName }
            { name: 'ENTRA_ALLOWED_USER_IDS', value: allowedUserIds }
            { name: 'MCP_SERVER_URL', value: mcpServerUrl }
            { name: 'MCP_TOKEN_RESOURCE', value: 'api://${mcpApiAppId}' }
            { name: 'AZURE_CLIENT_ID', value: identityClientId }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: 1
        rules: [
          { name: 'http', http: { metadata: { concurrentRequests: '5' } } }
        ]
      }
    }
  }
}

output hostname string = app.properties.configuration.ingress.fqdn
output resourceId string = app.id
