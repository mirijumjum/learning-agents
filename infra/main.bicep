// DevAssist on Azure: everything in the design guide's "what the subscription pays for" table.
//
// Deploy in two passes, because the app needs its secrets to exist first:
//   1. deployApp=false   creates the platform
//   2. store the secrets in Key Vault, push the image
//   3. deployApp=true    creates the app and the Teams bot
// See docs/GO-LIVE.md.

targetScope = 'resourceGroup'

@description('Short lowercase prefix for resource names, unique to you.')
@minLength(3)
@maxLength(12)
param namePrefix string

@description('EU region with GPT-5 Data Zone capacity.')
param location string = 'swedencentral'

@description('Second pass only: create the container app and the Teams bot.')
param deployApp bool = false

@description('Image to run, for example myacr.azurecr.io/devassist:1.0.0. Required when deployApp is true.')
param containerImage string = ''

@description('Base URL of your GitLab.')
param gitlabUrl string

@description('Comma-separated full paths of the pilot projects, for example acme/payments-api.')
param gitlabProjects string

@description('Microsoft Entra tenant whose members may use DevAssist.')
param tenantId string = subscription().tenantId

@description('Who receives the budget alerts.')
param budgetContactEmails array

@description('Monthly budget in USD.')
param monthlyBudgetUsd int = 100

@description('First day of a month, on or after the current one, formatted yyyy-MM-01.')
param budgetStartDate string

param strongModelName string = 'gpt-5'
param strongModelVersion string = '2025-08-07'
param fastModelName string = 'gpt-5-mini'
param fastModelVersion string = '2025-08-07'

@description('Capacity of each model deployment, in thousands of tokens per minute.')
param modelCapacity int = 50

@description('Keep at 1: replies are delivered by background work, which a scaled-to-zero replica would lose.')
param minReplicas int = 1

var suffix = uniqueString(resourceGroup().id)
var foundryName = '${namePrefix}-foundry-${suffix}'
var openAiEndpoint = 'https://${foundryName}.openai.azure.com/'

var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  cognitiveServicesOpenAiUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  logAnalyticsReader: '73c42c96-874c-492b-b04d-ab87d138a893'
}

// One identity for everything: the app has no passwords or API keys to leak.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-identity'
  location: location
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-insights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('${namePrefix}kv${suffix}', 24)
  location: location
  properties: {
    tenantId: tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: take('${namePrefix}acr${suffix}', 50)
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryName
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: foundryName
    allowProjectManagement: true
    disableLocalAuth: true // managed identity only
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: '${namePrefix}-devassist'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'DevAssist'
    description: 'Development agents published to Microsoft Teams.'
  }
}

// DataZoneStandard keeps inference inside the EU data zone.
resource strongModel 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: 'gpt-5'
  sku: { name: 'DataZoneStandard', capacity: modelCapacity }
  properties: {
    model: { format: 'OpenAI', name: strongModelName, version: strongModelVersion }
  }
}

resource fastModel 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: 'gpt-5-mini'
  sku: { name: 'DataZoneStandard', capacity: modelCapacity }
  properties: {
    model: { format: 'OpenAI', name: fastModelName, version: fastModelVersion }
  }
  dependsOn: [strongModel] // an account accepts one deployment change at a time
}

resource canPullImages 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, roles.acrPull)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource canUseModels 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, identity.id, roles.cognitiveServicesOpenAiUser)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.cognitiveServicesOpenAiUser)
  }
}

resource canReadSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, identity.id, roles.keyVaultSecretsUser)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
  }
}

// Read-only, as the design requires for the trace agent.
resource canReadLogs 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: logs
  name: guid(logs.id, identity.id, roles.logAnalyticsReader)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.logAnalyticsReader)
  }
}

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: '${namePrefix}-budget'
  properties: {
    category: 'Cost'
    amount: monthlyBudgetUsd
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      at50: { enabled: true, operator: 'GreaterThan', threshold: 50, contactEmails: budgetContactEmails }
      at80: { enabled: true, operator: 'GreaterThan', threshold: 80, contactEmails: budgetContactEmails }
      at100: { enabled: true, operator: 'GreaterThan', threshold: 100, contactEmails: budgetContactEmails }
    }
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = if (deployApp) {
  name: '${namePrefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployApp) {
  name: '${namePrefix}-app'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: { external: true, targetPort: 3978, transport: 'auto' }
      registries: [{ server: registry.properties.loginServer, identity: identity.id }]
      secrets: [
        { name: 'gitlab-token', keyVaultUrl: '${vault.properties.vaultUri}secrets/gitlab-token', identity: identity.id }
        { name: 'gitlab-webhook-secret', keyVaultUrl: '${vault.properties.vaultUri}secrets/gitlab-webhook-secret', identity: identity.id }
      ]
    }
    template: {
      containers: [
        {
          name: 'devassist'
          image: containerImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'DEVASSIST_PROFILE', value: 'azure' }
            { name: 'AZURE_OPENAI_ENDPOINT', value: openAiEndpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT_STRONG', value: strongModel.name }
            { name: 'AZURE_OPENAI_DEPLOYMENT_FAST', value: fastModel.name }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE', value: 'UserManagedIdentity' }
            { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID', value: identity.properties.clientId }
            { name: 'CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID', value: tenantId }
            { name: 'DEVASSIST_ALLOWED_TENANT_ID', value: tenantId }
            { name: 'GITLAB_URL', value: gitlabUrl }
            { name: 'GITLAB_PROJECTS', value: gitlabProjects }
            { name: 'GITLAB_TOKEN', secretRef: 'gitlab-token' }
            { name: 'GITLAB_WEBHOOK_SECRET', secretRef: 'gitlab-webhook-secret' }
            { name: 'DEVASSIST_LOG_STORE', value: 'azure' }
            { name: 'LOG_ANALYTICS_WORKSPACE_ID', value: logs.properties.customerId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
            { name: 'DEVASSIST_AUDIT_PATH', value: '/tmp/devassist-audit.jsonl' }
          ]
        }
      ]
      scale: { minReplicas: minReplicas, maxReplicas: 1 } // conversation state is in memory, so one replica
    }
  }
  dependsOn: [canPullImages, canUseModels, canReadSecrets, canReadLogs]
}

// Teams is a standard channel: unlimited messages, free.
resource bot 'Microsoft.BotService/botServices@2022-09-15' = if (deployApp) {
  name: '${namePrefix}-bot'
  location: 'global'
  kind: 'azurebot'
  sku: { name: 'F0' }
  properties: {
    displayName: 'DevAssist'
    endpoint: deployApp ? 'https://${app.properties.configuration.ingress.fqdn}/api/messages' : ''
    msaAppType: 'UserAssignedMSI'
    msaAppId: identity.properties.clientId
    msaAppMSIResourceId: identity.id
    msaAppTenantId: tenantId
    disableLocalAuth: true
  }
}

resource teamsChannel 'Microsoft.BotService/botServices/channels@2022-09-15' = if (deployApp) {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: { isEnabled: true }
  }
}

output botClientId string = identity.properties.clientId
output registryLoginServer string = registry.properties.loginServer
output keyVaultName string = vault.name
output foundryProjectName string = project.name
output appUrl string = deployApp ? 'https://${app.properties.configuration.ingress.fqdn}' : ''
