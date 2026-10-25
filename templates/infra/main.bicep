targetScope = 'resourceGroup'

@description('Existing landing-zone VNet. Review routing, address overlap, DNS and outbound access first.')
param vnetResourceGroup string
param vnetName string
param aksSubnetName string
param privateEndpointSubnetId string
param clusterName string
@minLength(5)
@maxLength(50)
param registryName string
param location string = resourceGroup().location
@description('A supported regional version from az aks get-versions; do not use an unreviewed default.')
param kubernetesVersion string
@minLength(1)
param adminGroupObjectIds array
@minValue(3)
param systemNodeCount int = 3
param systemVmSize string = 'Standard_D4ds_v5'
param availabilityZones array = ['1', '2', '3']
@description('Must not overlap VNet, peered networks, on-premises or service CIDR.')
param podCidr string
param serviceCidr string
param dnsServiceIP string

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: vnetName
  scope: resourceGroup(vnetResourceGroup)
}
resource control 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-control'
  location: location
}
resource kubelet 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${clusterName}-kubelet'
  location: location
}
resource operator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kubelet.id, control.id, 'identity-operator')
  scope: kubelet
  properties: {
    principalId: control.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'f1a07417-d97a-45cb-824c-7a7467783830')
  }
}
module network './network-role.bicep' = {
  name: '${clusterName}-subnet-access'
  scope: resourceGroup(vnetResourceGroup)
  params: {
    vnetName: vnetName
    subnetName: aksSubnetName
    principalId: control.properties.principalId
  }
}
resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: registryName
  location: location
  sku: { name: 'Premium' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Disabled'
    roleAssignmentMode: 'LegacyRegistryPermissions'
  }
}
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, kubelet.id, 'acr-pull')
  scope: registry
  properties: {
    principalId: kubelet.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
resource endpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${registryName}-endpoint'
  location: location
  properties: {
    subnet: { id: privateEndpointSubnetId }
    privateLinkServiceConnections: [{
      name: '${registryName}-registry'
      properties: {
        privateLinkServiceId: registry.id
        groupIds: ['registry']
      }
    }]
  }
}
resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.azurecr.io'
  location: 'global'
}
resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: '${clusterName}-acr-dns'
  location: 'global'
  properties: { virtualNetwork: { id: vnet.id }, registrationEnabled: false }
}
resource zoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{ name: 'registry', properties: { privateDnsZoneId: zone.id } }]
  }
}
resource cluster 'Microsoft.ContainerService/managedClusters@2025-07-01' = {
  name: clusterName
  location: location
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${control.id}': {} } }
  sku: { name: 'Base', tier: 'Standard' }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: clusterName
    enableRBAC: true
    disableLocalAccounts: true
    aadProfile: { managed: true, enableAzureRBAC: true, adminGroupObjectIDs: adminGroupObjectIds }
    apiServerAccessProfile: { enablePrivateCluster: true, enablePrivateClusterPublicFQDN: false, privateDNSZone: 'system' }
    oidcIssuerProfile: { enabled: true }
    securityProfile: { workloadIdentity: { enabled: true } }
    identityProfile: {
      kubeletidentity: { resourceId: kubelet.id, clientId: kubelet.properties.clientId, objectId: kubelet.properties.principalId }
    }
    agentPoolProfiles: [{
      name: 'system'
      count: systemNodeCount
      vmSize: systemVmSize
      mode: 'System'
      osType: 'Linux'
      osSKU: 'AzureLinux'
      type: 'VirtualMachineScaleSets'
      availabilityZones: availabilityZones
      vnetSubnetID: '${vnet.id}/subnets/${aksSubnetName}'
      upgradeSettings: { maxSurge: '33%' }
    }]
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkDataplane: 'cilium'
      networkPolicy: 'cilium'
      podCidr: podCidr
      serviceCidr: serviceCidr
      dnsServiceIP: dnsServiceIP
      outboundType: 'loadBalancer'
      loadBalancerSku: 'standard'
    }
  }
  dependsOn: [network, operator, pull]
}
output clusterResourceId string = cluster.id
output registryLoginServer string = registry.properties.loginServer
output oidcIssuer string = cluster.properties.oidcIssuerProfile.issuerURL
output kubeletPrincipalId string = kubelet.properties.principalId
