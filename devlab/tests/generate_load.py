import argparse
import itertools
import time
import json
import os
import yaml
import config as conf
import utils
import ConfigParser

from glanceclient.v1 import Client as glance
from keystoneclient import exceptions as ks_exceptions
from keystoneclient.v2_0 import client as keystone
from neutronclient.common import exceptions as nt_exceptions

from novaclient import exceptions as nv_exceptions
from novaclient.v1_1 import client as nova
from neutronclient.v2_0 import client as neutron
from cinderclient.v1 import client as cinder

from test_exceptions import NotFound

TIMEOUT = 600
VM_SPAWNING_LIMIT = 5
CREATE_CLEAN_METHODS_MAP = {
    'create_tenants': 'clean_tenants',
    'create_users': 'clean_users',
    'create_roles': 'clean_roles',
    'create_flavors': 'clean_flavors',
    'create_all_networking': 'clean_all_networking'
}
OPENSTACK_RELEASES = {'192.168.1.2': 'grizzly',
                      '192.168.1.3': 'icehouse',
                      '192.168.1.8': 'juno'}


def clean_if_exists(func):
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except (ks_exceptions.Conflict,
                nv_exceptions.Conflict,
                nt_exceptions.NeutronClientException) as e:
            print('Method "%s" failed, current resource already exists:\n%s'
                  % (func.__name__, e))
            clean_method = getattr(self.clean_tools,
                                   CREATE_CLEAN_METHODS_MAP[func.__name__])
            print 'Run cleanup method "%s"' % clean_method.__name__
            clean_method()
            print 'Run method "%s" one more time' % func.__name__
            func(self, *args, **kwargs)
    return wrapper


def retry_until_resources_created(resource_name):
    def actual_decorator(func):
        def wrapper(_list):
            for _ in range(TIMEOUT):
                _list = func(_list)
                if _list:
                    time.sleep(1)
                    continue
                else:
                    break
            else:
                msg = '{0}s with ids {1} have not become in active state'
                raise RuntimeError(msg.format(resource_name, _list))
        return wrapper
    return actual_decorator


def get_dict_from_config_file(config_file):
    conf_dict = {}
    for section in config_file.sections():
        conf_dict[section] = {}
        for option in config_file.options(section):
            conf_dict[section][option] = config_file.get(section, option)
    return conf_dict


class BasePrerequisites(object):

    def __init__(self, config, configuration_ini, cloud_prefix='SRC'):
        self.configuration_ini = configuration_ini
        self.filtering_utils = utils.FilteringUtils(
            self.configuration_ini['migrate']['filter_path'])
        self.migration_utils = utils.MigrationUtils(config)

        self.config = config
        self.cloud_prefix = cloud_prefix.lower()

        self.username = self.configuration_ini[self.cloud_prefix]['user']
        self.password = self.configuration_ini[self.cloud_prefix]['password']
        self.tenant = self.configuration_ini[self.cloud_prefix]['tenant']
        self.auth_url = self.configuration_ini[self.cloud_prefix]['auth_url']

        self.keystoneclient = keystone.Client(auth_url=self.auth_url,
                                              username=self.username,
                                              password=self.password,
                                              tenant_name=self.tenant)
        self.keystoneclient.authenticate()

        self.novaclient = nova.Client(username=self.username,
                                      api_key=self.password,
                                      project_id=self.tenant,
                                      auth_url=self.auth_url)

        self.token = self.keystoneclient.auth_token

        self.image_endpoint = \
            self.keystoneclient.service_catalog.get_endpoints(
                service_type='image',
                endpoint_type='publicURL')['image'][0]['publicURL']

        self.glanceclient = glance(endpoint=self.image_endpoint,
                                   token=self.token)

        self.neutronclient = neutron.Client(username=self.username,
                                            password=self.password,
                                            tenant_name=self.tenant,
                                            auth_url=self.auth_url)

        self.cinderclient = cinder.Client(self.username, self.password,
                                          self.tenant, self.auth_url)
        self.openstack_release = self._get_openstack_release()
        self.server_groups_supported = self.openstack_release in ['icehouse',
                                                                  'juno']

    def _get_openstack_release(self):
        for release in OPENSTACK_RELEASES:
            if release in self.auth_url:
                return OPENSTACK_RELEASES[release]
        raise RuntimeError('Unknown OpenStack release')

    def get_vagrant_vm_ip(self):
        for release in OPENSTACK_RELEASES:
            if release in self.auth_url:
                return release

    def get_tenant_id(self, tenant_name):
        for tenant in self.keystoneclient.tenants.list():
            if tenant.name == tenant_name:
                return tenant.id
        raise NotFound('Tenant with name "%s" was not found' % tenant_name)

    def get_tenant_name(self, tenant_id):
        for tenant in self.keystoneclient.tenants.list():
            if tenant.id == tenant_id:
                return tenant.name
        raise NotFound('Tenant with id "%s" was not found' % tenant_id)

    def get_user_id(self, user_name):
        for user in self.keystoneclient.users.list():
            if user.name == user_name:
                return user.id
        raise NotFound('User with name "%s" was not found' % user_name)

    def get_router_id(self, router):
        _router = self.neutronclient.list_routers(name=router)['routers']
        if _router:
            return _router[0]['id']
        raise NotFound('Router with name "%s" was not found' % router)

    def get_image_id(self, image_name):
        for image in self.glanceclient.images.list():
            if image.name == image_name:
                return image.id
        raise NotFound('Image with name "%s" was not found' % image_name)

    def get_flavor_id(self, flavor_name):
        for flavor in self.novaclient.flavors.list():
            if flavor.name == flavor_name:
                return flavor.id
        raise NotFound('Flavor with name "%s" was not found' % flavor_name)

    def get_vm_id(self, vm_name):
        for vm in self.novaclient.servers.list(search_opts={'all_tenants': 1}):
            if vm.name == vm_name:
                return vm.id
        raise NotFound('VM with name "%s" was not found' % vm_name)

    def get_role_id(self, role_name):
        for role in self.keystoneclient.roles.list():
            if role.name == role_name:
                return role.id
        raise NotFound('Role with name "%s" was not found' % role_name)

    def get_net_id(self, net):
        _net = self.neutronclient.list_networks(
            name=net, all_tenants=True)['networks']
        if _net:
            return _net[0]['id']
        raise NotFound('Network with name "%s" was not found' % net)

    def get_net_name(self, net_id):
        _net = self.neutronclient.list_networks(id=net_id,
                                                all_tenants=True)['networks']
        if _net:
            return _net[0]['name']
        raise NotFound('Network with id "%s" was not found' % net_id)

    def get_sg_id(self, sg):
        _sg = self.neutronclient.list_security_groups(
            name=sg, all_tenants=True)['security_groups']
        if _sg:
            return _sg[0]['id']
        raise NotFound('Security group with name "%s" was not found' % sg)

    def get_server_group_id(self, server_group_name):
        for server_group in self.get_all_server_groups():
            if server_group.name == server_group_name:
                return server_group.id
        msg = 'Server group with name "%s" was not found'
        raise NotFound(msg % server_group_name)

    def get_volume_id(self, volume_name):
        volumes = self.cinderclient.volumes.list(
            search_opts={'all_tenants': 1})
        for volume in volumes:
            if volume.display_name == volume_name:
                return volume.id
        raise NotFound('Volume with name "%s" was not found' % volume_name)

    def get_volume_snapshot_id(self, snapshot_name):
        snapshots = self.cinderclient.volume_snapshots.list(
            search_opts={'all_tenants': 1})
        for snapshot in snapshots:
            if snapshot.display_name == snapshot_name:
                return snapshot.id
        raise NotFound('Snapshot with name "%s" was not found' % snapshot_name)

    def get_user_tenant_roles(self, user):
        user_tenant_roles = []
        for tenant in self.keystoneclient.tenants.list():
            user_tenant_roles.extend(self.keystoneclient.roles.roles_for_user(
                user=self.get_user_id(user.name),
                tenant=self.get_tenant_id(tenant.name)))
        return user_tenant_roles

    def get_ext_routers(self):
        routers = self.neutronclient.list_routers()['routers']
        ext_routers = [router for router in routers
                       if router['external_gateway_info']]
        return ext_routers

    def get_sec_group_id_by_tenant_id(self, tenant_id):
        sec_group_list = self.neutronclient.list_security_groups()
        return [i['id'] for i in sec_group_list['security_groups']
                if i['tenant_id'] == tenant_id]

    def get_users_keypairs(self):
        self.switch_user(self.username, self.password, self.tenant)
        user_names = [u['name'] for u in self.config.users]
        keypairs = []
        for user in self.config.users:
            if user['name'] not in user_names:
                continue
            if not user.get('enabled') or user.get('deleted'):
                continue
            if not self.tenant_exists(user['tenant']) or \
                    not self.user_has_not_primary_tenants(user['name']):
                continue
            try:
                self.switch_user(user['name'], user['password'],
                                 user['tenant'])
            except ks_exceptions.Unauthorized:
                self.keystoneclient.users.update(
                    self.get_user_id(user['name']), password=user['password'],
                    tenant=user['tenant'])
                self.switch_user(user['name'], user['password'],
                                 user['tenant'])
            keypairs.extend(self.novaclient.keypairs.list())
        return keypairs

    def get_all_server_groups(self):
        initial_tenant = self.keystoneclient.tenant_name
        self.switch_user(self.username, self.password, self.tenant)
        server_groups = self.novaclient.server_groups.list()
        for tenant in self.config.tenants:
            if not self.tenant_exists(tenant['name']):
                continue
            with utils.AddAdminUserRoleToNonAdminTenant(
                    self.keystoneclient, self.username, tenant['name']):
                self.switch_user(self.username, self.password,
                                 tenant['name'])
                server_groups.extend(self.novaclient.server_groups.list())
        self.switch_user(self.username, self.password, initial_tenant)
        return server_groups

    def user_has_not_primary_tenants(self, user_name):
        user_id = self.get_user_id(user_name)
        for tenant in self.keystoneclient.tenants.list():
            if self.keystoneclient.roles.roles_for_user(user=user_id,
                                                        tenant=tenant.id):
                return True
        return False

    def check_vm_state(self, srv):
        srv = self.novaclient.servers.get(srv)
        return srv.status == 'ACTIVE'

    def tenant_exists(self, tenant_name):
        self.switch_user(self.username, self.password, self.tenant)
        try:
            self.get_tenant_id(tenant_name)
            return True
        except NotFound:
            return False

    def switch_user(self, user, password, tenant):
        self.keystoneclient = keystone.Client(auth_url=self.auth_url,
                                              username=user,
                                              password=password,
                                              tenant_name=tenant)
        self.keystoneclient.authenticate()
        self.token = self.keystoneclient.auth_token
        self.novaclient = nova.Client(username=user,
                                      api_key=password,
                                      project_id=tenant,
                                      auth_url=self.auth_url)

        self.glanceclient = glance(endpoint=self.image_endpoint,
                                   token=self.token)

        self.neutronclient = neutron.Client(username=user,
                                            password=password,
                                            tenant_name=tenant,
                                            auth_url=self.auth_url)

        self.cinderclient = cinder.Client(user, password, tenant,
                                          self.auth_url)


class Prerequisites(BasePrerequisites):

    def __init__(self, config, configuration_ini, cloud_prefix='SRC'):
        super(Prerequisites, self).__init__(config,
                                            configuration_ini,
                                            cloud_prefix)
        # will be filled during create all networking step
        self.ext_net_id = None
        # object of Prerequisites for dst cloud
        self.dst_cloud = None
        self.clean_tools = CleanEnv(config, configuration_ini, cloud_prefix)

    @clean_if_exists
    def create_users(self, users=None):
        def get_params_for_user_creating(_user):
            if 'tenant' in _user:
                _user['tenant_id'] = self.get_tenant_id(_user['tenant'])
            params = ['name', 'password', 'email', 'enabled', 'tenant_id']
            return {param: _user[param] for param in params if param in _user}

        if users is None:
            users = self.config.users
        for user in users:
            self.keystoneclient.users.create(
                **get_params_for_user_creating(user))
            if not user.get('additional_tenants'):
                continue
            for tenant in user['additional_tenants']:
                self.keystoneclient.roles.add_user_role(
                    tenant=self.get_tenant_id(tenant['name']),
                    role=self.get_role_id(tenant['role']),
                    user=self.get_user_id(user['name']))
            # Next action for default sec group creation
            if not user['enabled'] or 'tenant' not in user:
                continue
            self.switch_user(user=user['name'], password=user['password'],
                             tenant=user['tenant'])
            self.novaclient.security_groups.list()
            self.switch_user(user=self.username, password=self.password,
                             tenant=self.tenant)

    @clean_if_exists
    def create_roles(self):
        for role in self.config.roles:
            self.keystoneclient.roles.create(name=role['name'])

    def create_user_tenant_roles(self, user_tenant_roles=None):
        if user_tenant_roles is None:
            user_tenant_roles = self.config.user_tenant_roles
        for user_roles in user_tenant_roles:
            for user, roles in user_roles.iteritems():
                user = self.get_user_id(user)
                for role in roles:
                    try:
                        self.keystoneclient.roles.add_user_role(
                            user=user, role=self.get_role_id(role['role']),
                            tenant=self.get_tenant_id(role['tenant']))
                    except ks_exceptions.Conflict as e:
                        print "There was an error during role creating: {}"\
                            .format(e)
                        continue

    @clean_if_exists
    def create_tenants(self, tenants=None):
        if tenants is None:
            tenants = self.config.tenants
        for tenant in tenants:
            self.keystoneclient.tenants.create(tenant_name=tenant['name'],
                                               description=tenant[
                                                   'description'],
                                               enabled=tenant['enabled'])
            self.keystoneclient.roles.add_user_role(
                self.get_user_id(self.username),
                self.get_role_id('admin'),
                self.get_tenant_id(tenant['name']))

    def create_keypairs(self):
        for keypair in self.config.keypairs:
            for _user in self.config.users:
                if _user['name'] == keypair['user']:
                    user = _user
                    break
            else:
                msg = 'User for keypair %s was not found'
                raise RuntimeError(msg % keypair['name'])
            self.switch_user(user=user['name'], password=user['password'],
                             tenant=user['tenant'])
            self.novaclient.keypairs.create(name=keypair['name'],
                                            public_key=keypair['public_key'])
        self.switch_user(user=self.username, password=self.password,
                         tenant=self.tenant)

    def modify_quotas(self):
        for tenant in self.config.tenants:
            if 'quota' in tenant:
                self.novaclient.quotas.update(tenant_id=self.get_tenant_id(
                    tenant['name']), **tenant['quota'])

    def upload_image(self):
        @retry_until_resources_created('image')
        def wait_until_images_created(image_ids):
            for img_id in image_ids[:]:
                img = self.glanceclient.images.get(img_id)
                if img.status == 'active':
                    image_ids.remove(img_id)
            return image_ids

        def _get_body_for_image_creating(_image):
            # Possible parameters for image creating
            params = ['name', 'location', 'disk_format', 'container_format',
                      'is_public', 'copy_from']
            return {param: _image[param] for param in params
                    if param in _image}

        img_ids = []
        for tenant in self.config.tenants:
            if not tenant.get('images'):
                continue
            for image in tenant['images']:

                self.switch_user(user=self.username, password=self.password,
                                 tenant=tenant['name'])

                img = self.glanceclient.images.create(
                    **_get_body_for_image_creating(image))

                img_ids.append(img.id)
        self.switch_user(user=self.username, password=self.password,
                         tenant=self.tenant)

        for image in self.config.images:
            img = self.glanceclient.images.create(
                **_get_body_for_image_creating(image))
            img_ids.append(img.id)
        wait_until_images_created(img_ids)
        src_cloud = Prerequisites(cloud_prefix='SRC',
                                  configuration_ini=self.configuration_ini,
                                  config=self.config)
        src_img = [x.__dict__ for x in
                   src_cloud.glanceclient.images.list()]
        for image in src_img:
            if image['name'] in self.config.img_to_add_members:
                image_id = image['id']
                tenant_list = self.keystoneclient.tenants.list()
                for tenant in tenant_list:
                    tenant = tenant.__dict__
                    if tenant['name'] in self.config.members:
                        member_id = tenant['id']
                        self.glanceclient.image_members.create(image_id,
                                                               member_id)
        if getattr(self.config, 'create_zero_image', None):
            self.glanceclient.images.create()

    def update_filtering_file(self):
        src_cloud = Prerequisites(cloud_prefix='SRC',
                                  configuration_ini=self.configuration_ini,
                                  config=self.config)
        src_img = [x.__dict__ for x in
                   src_cloud.glanceclient.images.list()]
        src_vms = [x.__dict__ for x in
                   src_cloud.novaclient.servers.list(
                       search_opts={'all_tenants': 1})]
        image_dict = {}
        for image in src_img:
            img_members = self.glanceclient.image_members.list(image['id'])
            if len(img_members) > 1:
                img_mem_list = []
                for img_member in img_members:
                    img_member = img_member.__dict__
                    img_mem_list.append(img_member['member_id'])
                image_dict[image['id']] = img_mem_list
        vm_id_list = []
        for vm in src_vms:
            vm_id = vm['id']
            vm_id_list.append(vm_id)
        filter_dict = {
            'tenants': {
                'tenant_id': []
            },
            'instances': {
                'id': []
            },
            'images': {
                'images_list': []
            }
        }
        all_img_ids = []
        not_incl_img = []
        for image in src_img:
            all_img_ids.append(image['id'])
        for img in self.config.images_not_included_in_filter:
            not_incl_img.append(self.get_image_id(img))
        for key in filter_dict.keys():
            if key == 'images':
                for img_id in all_img_ids:
                    if img_id not in not_incl_img:
                        filter_dict[key]['images_list'].append(str(img_id))
            elif key == 'instances':
                for vm in vm_id_list:
                    if vm != self.get_vm_id('not_in_filter'):
                        filter_dict[key]['id'].append(str(vm))
        for tenant in self.config.tenants + [{'name': self.tenant}]:
            if tenant.get('deleted'):
                continue
            filter_dict['tenants']['tenant_id'] = [str(
                self.get_tenant_id(tenant['name']))]
            file_path = self.config.filters_file_naming_template.format(
                tenant_name=tenant['name'])
            with open(file_path, "w+") as f:
                yaml.dump(filter_dict, f, default_flow_style=False)

    @clean_if_exists
    def create_flavors(self):
        for flavor in self.config.flavors:
            fl = self.novaclient.flavors.create(**flavor)
            if flavor.get('is_public') == False:
                self.novaclient.flavor_access.add_tenant_access(
                    flavor=fl.id,
                    tenant=self.get_tenant_id(self.tenant))
        for tenant in self.config.tenants:
            if tenant.get('flavors'):
                for flavor in tenant['flavors']:
                    fl = self.novaclient.flavors.create(**flavor)
                    if flavor.get('is_public') == False:
                        self.novaclient.flavor_access.add_tenant_access(
                            flavor=fl.id,
                            tenant=self.get_tenant_id(tenant['name']))

    def _get_parameters_for_vm_creating(self, vm):
        def get_vm_nics(_vm):
            if 'nics' in _vm:
                for _nic in _vm['nics']:
                    _nic['net-id'] = self.get_net_id(_nic['net-id'])
                return _vm['nics']
            nets = self.neutronclient.list_networks()['networks']
            _t = self.keystoneclient.tenant_id
            nics = [{'net-id': net['id']} for net in nets
                    if not net['router:external'] and net['tenant_id'] == _t]
            return nics
        image_id = self.get_image_id(vm['image']) if vm.get('image') else ''
        params = {'image': image_id,
                  'flavor': self.get_flavor_id(vm['flavor']),
                  'nics': get_vm_nics(vm),
                  'name': vm['name'],
                  'key_name': vm.get('key_name')}
        if 'server_group' in vm and self.server_groups_supported:
            params['scheduler_hints'] = {'group': self.get_server_group_id(
                vm['server_group'])}
        return params

    def create_server_groups(self):
        def _create_groups(server_groups_list):
            for server_group in server_groups_list:
                self.novaclient.server_groups.create(**server_group)
        # Create server group for admin tenant
        _create_groups(self.config.server_groups)
        for tenant in self.config.tenants:
            if not tenant.get('server_groups'):
                continue
            self.switch_user(self.username, self.password, tenant['name'])
            _create_groups(tenant['server_groups'])
        self.switch_user(self.username, self.password, self.tenant)

    def create_vms(self):

        def wait_for_vm_creating():
            """ When limit for creating vms in nova is reached, we receive
                exception from nova: 'novaclient.exceptions.OverLimit:
                This request was rate-limited. (HTTP 413)'. To handle this we
                set limit for vm spawning.
            """
            for _ in range(TIMEOUT):
                all_vms = self.novaclient.servers.list(
                    search_opts={'all_tenants': 1})
                spawning_vms = [vm.id for vm in all_vms
                                if vm.status == 'BUILD']
                if len(spawning_vms) >= VM_SPAWNING_LIMIT:
                    time.sleep(1)
                else:
                    break
            else:
                raise RuntimeError(
                    'VMs with ids {0} were in "BUILD" state more than {1} '
                    'seconds'.format(spawning_vms, TIMEOUT))

        def create_vms(vm_list):
            vm_ids = []
            for vm in vm_list:
                wait_for_vm_creating()
                _vm = self.novaclient.servers.create(
                    **self._get_parameters_for_vm_creating(vm))
                vm_ids.append(_vm.id)
                if not vm.get('fip'):
                    continue
                wait_until_vms_created([_vm.id])
                fip = self.neutronclient.create_floatingip(
                    {"floatingip": {"floating_network_id": self.ext_net_id}})
                _vm.add_floating_ip(fip['floatingip']['floating_ip_address'])
            return vm_ids

        @retry_until_resources_created('vm')
        def wait_until_vms_created(vm_list):
            for vm in vm_list[:]:
                if self.check_vm_state(vm):
                    vm_list.remove(vm)
            return vm_list

        vms = create_vms(self.config.vms)
        for tenant in self.config.tenants:
            if not tenant.get('vms'):
                continue

            # To create vm with proper keypair, need to switch to right user
            keypairs = set([vm['key_name'] for vm in tenant['vms']
                            if vm.get('key_name')])
            # Split all vms on with and without keypair
            keypairs_vms = {keypair: [] for keypair in keypairs}
            vms_wo_keypairs = []
            for vm in tenant['vms']:
                if vm.get('key_name'):
                    keypairs_vms[vm['key_name']].append(vm)
                else:
                    vms_wo_keypairs.append(vm)
            for keypair in keypairs_vms:
                username = [kp['user'] for kp in self.config.keypairs
                            if kp['name'] == keypair][0]
                user = [user for user in self.config.users
                        if user['name'] == username][0]
                if user['tenant'] != tenant['name']:
                    msg = 'Keypair "{0}" not accessible from tenant "{1}"'
                    raise RuntimeError(msg.format(keypair[0], tenant['name']))
                self.switch_user(user=user['name'], password=user['password'],
                                 tenant=tenant['name'])
                vms.extend(create_vms(keypairs_vms[keypair]))
            # Create vms without keypair
            user = [u for u in self.config.users
                    if u.get('tenant') == tenant['name']][0]
            self.switch_user(user=user['name'], password=user['password'],
                             tenant=tenant['name'])
            vms.extend(create_vms(vms_wo_keypairs))
        self.switch_user(user=self.username, password=self.password,
                         tenant=self.tenant)

        wait_until_vms_created(vms)

    def create_vm_snapshots(self):
        @retry_until_resources_created('vm_snapshot')
        def wait_until_vm_snapshots_created(snapshot_ids):
            for snp_id in snapshot_ids[:]:
                snp = self.glanceclient.images.get(snp_id)
                if snp.status == 'active':
                    snp_ids.remove(snp_id)
                elif snp.status == 'error':
                    msg = 'Snapshot with id {0} has become in error state'
                    raise RuntimeError(msg.format(snp_id))
            return snapshot_ids

        snp_ids = []
        for snapshot in self.config.snapshots:
            self.novaclient.servers.create_image(
                server=self.get_vm_id(snapshot['server']),
                image_name=snapshot['image_name'])
            snp = self.glanceclient.images.get(self.get_image_id(
                snapshot['image_name']))
            snp_ids.append(snp.id)
        wait_until_vm_snapshots_created(snp_ids)

    def create_networks(self, networks):

        def get_body_for_network_creating(_net):
            # Possible parameters for network creating
            params = ['name', 'admin_state_up', 'shared', 'router:external',
                      'provider:network_type', 'provider:segmentation_id',
                      'provider:physical_network']
            return {param: _net[param] for param in params if param in _net}

        def get_body_for_subnet_creating(_subnet):
            # Possible parameters for subnet creating
            params = ['name', 'cidr', 'allocation_pools', 'dns_nameservers',
                      'host_routes', 'ip_version', 'network_id']
            return {param: _subnet[param] for param in params
                    if param in _subnet}

        for network in networks:
            net = self.neutronclient.create_network(
                {'network': get_body_for_network_creating(network)})
            for subnet in network['subnets']:
                subnet['network_id'] = net['network']['id']
                _subnet = self.neutronclient.create_subnet(
                    {'subnet': get_body_for_subnet_creating(subnet)})
                self.neutronclient.create_port(
                    {"port": {"network_id": net['network']['id']}})
                # If network has attribute routers_to_connect, interface to
                # this network is created for given router.
                # If network has attribute set_as_gateway_for_routers, it will
                # be set as router's gateway.
                if subnet.get('routers_to_connect') is not None:
                    for router in subnet['routers_to_connect']:
                        router_id = self.get_router_id(router)
                        self.neutronclient.add_interface_router(
                            router_id, {"subnet_id": _subnet['subnet']['id']})
                if network.get('router:external') and \
                        subnet.get('set_as_gateway_for_routers') is not None:
                    for router in subnet['set_as_gateway_for_routers']:
                        router_id = self.get_router_id(router)
                        self.neutronclient.add_gateway_router(
                            router_id, {"network_id": net['network']['id']})

    def create_routers(self):
        for router in self.config.routers:
            self.neutronclient.create_router(router)
        for tenant in self.config.tenants:
            if tenant.get('routers'):
                self.switch_user(user=self.username, password=self.password,
                                 tenant=tenant['name'])
                for router in tenant['routers']:
                    self.neutronclient.create_router(router)
        self.switch_user(user=self.username, password=self.password,
                         tenant=self.tenant)

    def get_subnet_id(self, name):
        subs = self.neutronclient.list_subnets()['subnets']
        for sub in subs:
            if sub['name'] == name:
                return sub['id']

    def get_pool_id(self, name):
        pools = self.neutronclient.list_pools()['pools']
        for pool in pools:
            if pool['name'] == name:
                return pool['id']

    def create_pools(self, pools):
        for pool in pools:
            pool["tenant_id"] = self.get_tenant_id(pool["tenant_name"])
            pool["subnet_id"] = self.get_subnet_id(pool["subnet_name"])
            pool = {i: v for i, v in pool.iteritems()
                    if i not in ["tenant_name", "subnet_name"]}
            self.neutronclient.create_pool({'pool': pool})

    def create_members(self, members):
        for member in members:
            member["pool_id"] = self.get_pool_id(member["pool_name"])
            member["tenant_id"] = self.get_tenant_id(member["tenant_name"])
            member = {i: v for i, v in member.iteritems()
                      if i not in ["pool_name", "tenant_name"]}
            self.neutronclient.create_member({"member": member})

    def create_monitors(self, monitors):
        for mon in monitors:
            mon["tenant_id"] = self.get_tenant_id(mon["tenant_name"])
            mon = {i: v for i, v in mon.iteritems()
                   if i not in ["tenant_name"]}
            self.neutronclient.create_health_monitor({"health_monitor": mon})

    def create_vips(self, vips):
        for vip in vips:
            vip["pool_id"] = self.get_pool_id(vip["pool_name"])
            vip["tenant_id"] = self.get_tenant_id(vip["tenant_name"])
            vip["subnet_id"] = self.get_subnet_id(vip["subnet_name"])
            vip = {i: v for i, v in vip.iteritems()
                   if i not in ["tenant_name", "pool_name", "subnet_name"]}
            self.neutronclient.create_vip({"vip": vip})

    @clean_if_exists
    def create_all_networking(self):
        self.create_routers()
        self.create_networks(self.config.networks)
        # Getting ip address for real network. This networks will be used to
        # allocation floating ips.
        self.ext_net_id = self.get_net_id(
            [n['name'] for n in self.config.networks
             if n.get('real_network')][0])
        self.create_pools(self.config.pools)
        self.create_members(self.config.members_lbaas)
        self.create_monitors(self.config.monitors)
        self.create_vips(self.config.vips)

        for tenant in self.config.tenants:
            if tenant.get('networks'):
                self.switch_user(user=self.username, password=self.password,
                                 tenant=tenant['name'])
                self.create_networks(tenant['networks'])
            if tenant.get('pools'):
                self.create_pools(tenant['pools'])
            if tenant.get('members_lbaas'):
                self.create_members(tenant['members_lbaas'])
            if tenant.get('monitors'):
                self.create_monitors(tenant['monitors'])
            if tenant.get('vips'):
                self.create_vips(tenant['vips'])
            if not tenant.get('unassociated_fip'):
                continue
            for _ in range(tenant['unassociated_fip']):
                self.neutronclient.create_floatingip(
                    {"floatingip": {"floating_network_id": self.ext_net_id}})
        self.switch_user(user=self.username, password=self.password,
                         tenant=self.tenant)

    def create_security_group(self, sg_list):
        for security_group in sg_list:
            gid = self.novaclient.security_groups.create(
                name=security_group['name'],
                description=security_group['description']).id
            if 'rules' in security_group:
                for rule in security_group['rules']:
                    self.novaclient.security_group_rules.create(
                        gid,
                        ip_protocol=rule['ip_protocol'],
                        from_port=rule['from_port'], to_port=rule['to_port'],
                        cidr=rule['cidr'])

    def create_security_groups(self):
        for tenant in self.config.tenants:
            if 'security_groups' in tenant:
                self.switch_user(user=self.username, password=self.password,
                                 tenant=tenant['name'])
                self.create_security_group(tenant['security_groups'])
        self.switch_user(user=self.username, password=self.password,
                         tenant=self.tenant)

    def create_security_group_rule(self, group_id, tenant_id, protocol='tcp',
                                   port_range_min=22, port_range_max=22,
                                   direction='ingress'):
        sec_rule = {'security_group_rule': {"security_group_id": group_id,
                                            "protocol": protocol,
                                            "direction": direction,
                                            'tenant_id': tenant_id,
                                            "port_range_min": port_range_min,
                                            "port_range_max": port_range_max}}
        self.neutronclient.create_security_group_rule(sec_rule)

    def create_cinder_volumes(self, volumes_list):

        @retry_until_resources_created('volume')
        def wait_for_volumes(volume_ids):
            for volume_id in volume_ids[:]:
                _vlm = self.cinderclient.volumes.get(volume_id)
                if _vlm.status == 'available' or _vlm.status == 'in-use':
                    volume_ids.remove(volume_id)
                elif _vlm.status == 'error':
                    msg = 'Volume with id {0} was created with error'
                    raise RuntimeError(msg.format(volume_id))
            return volume_ids

        def wait_until_vms_with_fip_accessible(_vm_id):
            vm = self.novaclient.servers.get(_vm_id)
            self.migration_utils.open_ssh_port_secgroup(self, vm.tenant_id)
            try:
                fip_addr = self.migration_utils.get_vm_fip(vm)
            except RuntimeError:
                return
            self.migration_utils.wait_until_vm_accessible_via_ssh(fip_addr)

        def get_params_for_volume_creating(_volume):
            params = ['display_name', 'size', 'imageRef']
            vt_exists = 'volume_type' in _volume and \
                [vt for vt in self.cinderclient.volume_types.list()
                 if vt.name == _volume['volume_type']]
            if vt_exists:
                params.append('volume_type')
            if 'image' in _volume:
                _volume['imageRef'] = self.get_image_id(_volume['image'])
            return {param: _volume[param] for param in params
                    if param in _volume}

        vlm_ids = []
        for volume in volumes_list:
            if 'user' in volume:
                user = [u for u in self.config.users
                        if u['name'] == volume['user']][0]
                self.switch_user(user=user['name'], password=user['password'],
                                 tenant=user['tenant'])
            vlm = self.cinderclient.volumes.create(
                **get_params_for_volume_creating(volume))
            vlm_ids.append(vlm.id)
        self.switch_user(user=self.username, password=self.password,
                         tenant=self.tenant)
        wait_for_volumes(vlm_ids)
        vlm_ids = []
        for volume in volumes_list:
            if 'server_to_attach' not in volume:
                continue
            vlm_id = self.get_volume_id(volume['display_name'])
            vm_id = self.get_vm_id(volume['server_to_attach'])
            # To correct attaching volume, vm should be fully ready
            wait_until_vms_with_fip_accessible(vm_id)
            self.novaclient.volumes.create_server_volume(
                server_id=vm_id, volume_id=vlm_id, device=volume['device'])
            vlm_ids.append(vlm_id)
        wait_for_volumes(vlm_ids)

    def create_cinder_snapshots(self, snapshot_list):
        for snapshot in snapshot_list:
            self.cinderclient.volume_snapshots.create(**snapshot)

    def create_cinder_objects(self):
        self.create_cinder_volumes(self.config.cinder_volumes)
        self.create_cinder_snapshots(self.config.cinder_snapshots)
        for tenant in self.config.tenants:
            if 'cinder_volumes' in tenant:
                self.switch_user(user=self.username, password=self.password,
                                 tenant=tenant['name'])
                self.create_cinder_volumes(tenant['cinder_volumes'])
                if 'cinder_snapshots' in tenant:
                    self.create_cinder_snapshots(tenant['cinder_snapshots'])
        self.switch_user(user=self.username, password=self.password,
                         tenant=self.tenant)

    def write_data_to_volumes(self):
        """Method creates file and md5sum of this file on volume
        """
        volumes = self.config.cinder_volumes
        volumes += itertools.chain(*[tenant['cinder_volumes'] for tenant
                                     in self.config.tenants if 'cinder_volumes'
                                     in tenant])
        for volume in volumes:
            attached_volume = volume.get('server_to_attach')
            if not volume.get('write_to_file') or not attached_volume:
                continue
            if not volume.get('mount_point'):
                msg = 'Please specify mount point for volume %s'
                raise RuntimeError(msg % volume['name'])

            vm = self.novaclient.servers.get(
                self.get_vm_id(volume['server_to_attach']))
            vm_ip = self.migration_utils.get_vm_fip(vm)
            # Make filesystem on volume. The OS assigns the volume to the next
            # available device, which /dev/vda
            cmd = '/usr/sbin/mkfs.ext2 /dev/vdb'
            self.migration_utils.execute_command_on_vm(vm_ip, cmd)
            # Create directory for mount point
            cmd = 'mkdir -p %s' % volume['mount_point']
            self.migration_utils.execute_command_on_vm(vm_ip, cmd)
            # Mount volume
            cmd = 'mount {0} {1}'.format(volume['device'],
                                         volume['mount_point'])
            self.migration_utils.execute_command_on_vm(vm_ip, cmd)
            for _file in volume['write_to_file']:
                _path, filename = os.path.split(_file['filename'])
                path = '{0}/{1}'.format(volume['mount_point'], _path)
                if _path:
                    cmd = 'mkdir -p {path}'.format(path=path)
                    self.migration_utils.execute_command_on_vm(vm_ip, cmd)
                cmd = 'sh -c "echo \'{content}\' > {path}/{filename}"'
                self.migration_utils.execute_command_on_vm(vm_ip, cmd.format(
                    path=path, content=_file['data'], filename=filename))
                cmd = 'sh -c "md5sum {path}/{_file} > {path}/{_file}_md5"'
                self.migration_utils.execute_command_on_vm(vm_ip, cmd.format(
                    path=path, _file=filename))

    def create_invalid_cinder_objects(self):
        invalid_volume_tmlt = 'cinder_volume_%s'
        volumes = [
            {
                'display_name': invalid_volume_tmlt % st,
                'size': 1,
            }
            for st in self.config.INVALID_STATUSES
        ]
        existing = [vol.display_name
                    for vol in self.cinderclient.volumes.list(
                        search_opts={'all_tenants': 1})]
        volumes = [vol
                   for vol in volumes if vol['display_name'] not in existing]
        if volumes:
            self.create_cinder_volumes(volumes)
        for st in self.config.INVALID_STATUSES:
            vol = self.cinderclient.volumes.find(
                display_name=invalid_volume_tmlt % st)
            self.cinderclient.volumes.reset_state(vol, state=st)

    def emulate_vm_states(self):
        for vm_state in self.config.vm_states:
            # emulate error state:
            if vm_state['state'] == u'error':
                self.novaclient.servers.reset_state(
                    server=self.get_vm_id(vm_state['name']),
                    state=vm_state['state'])
            # emulate suspend state:
            elif vm_state['state'] == u'suspend':
                self.novaclient.servers.suspend(self.get_vm_id(
                    vm_state['name']))
            # emulate resize state:
            elif vm_state['state'] == u'pause':
                self.novaclient.servers.pause(self.get_vm_id(vm_state['name']))
            # emulate stop/shutoff state:
            elif vm_state['state'] == u'stop':
                self.novaclient.servers.stop(self.get_vm_id(vm_state['name']))
            # emulate resize state:
            elif vm_state['state'] == u'resize':
                self.novaclient.servers.resize(
                    self.get_vm_id(vm_state['name']), '2')

    def generate_vm_state_list(self):
        data = {}
        for vm in self.novaclient.servers.list(search_opts={'all_tenants': 1}):
            for _ in range(TIMEOUT):
                _vm = self.novaclient.servers.get(vm.id)
                if _vm.status != u'RESIZE':
                    break
                time.sleep(1)
            vm_state = self.novaclient.servers.get(vm.id).status
            data[vm.name] = vm_state

        file_name = 'pre_migration_vm_states.json'
        with open(file_name, 'w') as outfile:
            json.dump(data, outfile, sort_keys=True, indent=4,
                      ensure_ascii=False)

    def delete_flavor(self, flavor='del_flvr'):
        """
        Method for flavor deletion.
        """
        try:
            self.novaclient.flavors.delete(
                self.get_flavor_id(flavor))
        except nv_exceptions.ClientException as e:
            print "Flavor %s failed to delete: %s" % (flavor, repr(e))

    def update_network_quotas(self):
        tenants = {ten.name: ten.id
                   for ten in self.keystoneclient.tenants.list()}
        for tenant in self.config.tenants:
            if "quota_network" not in tenant:
                continue
            ten_id = tenants[tenant["name"]]
            quota_net = tenant["quota_network"]
            self.neutronclient.update_quota(ten_id,
                                            {"quota": quota_net})

    def modify_admin_tenant_quotas(self):
        for tenant in self.config.tenants:
            if 'quota' in tenant:
                self.novaclient.quotas.update(tenant_id=self.get_tenant_id(
                    self.tenant), **tenant['quota'])
                break

    def change_admin_role_in_tenants(self):
        for tenant in self.config.tenants:
            self.keystoneclient.roles.remove_user_role(
                self.get_user_id(self.username),
                self.get_role_id('admin'),
                self.get_tenant_id(tenant['name']))
            self.switch_user(self.username, self.password, self.tenant)

    def delete_users(self):
        for user in self.config.users:
            if user.get('deleted'):
                self.keystoneclient.users.delete(
                    self.get_user_id(user['name']))

    def delete_tenants(self):
        for tenant in self.config.tenants:
            if tenant.get('deleted'):
                self.keystoneclient.tenants.delete(
                    self.get_tenant_id(tenant['name']))

    def create_tenant_wo_sec_group_on_dst(self):
        """
        Method for check fixed issue, when on dst tenant does not have
        security group, even default, while on src this tenant has security
        group.
        """
        self.dst_cloud = Prerequisites(
            cloud_prefix='DST',
            configuration_ini=self.configuration_ini,
            config=self.config)
        for t in self.config.tenants:
            if not t.get('deleted') and t['enabled']:
                try:
                    self.dst_cloud.keystoneclient.tenants.create(
                        tenant_name=t['name'], description=t['description'],
                        enabled=t['enabled'])
                except ks_exceptions.Conflict:
                    pass

    def create_user_on_dst(self):
        """
        Method for check fixed issue, when on dst and src exists user with
        same user tenant role.
        1. Get one user tenant role from config
        2. Get user from config, which should be created
        3. Create roles, which specified in user tenant roles
        4. Get tenants, in which user has roles and tenant which the user
         belongs
        5. Create tenants
        6. Create user
        7. Create user tenant roles
        """
        user_tenant_role = self.config.user_tenant_roles[0]
        username, roles_to_create = user_tenant_role.items()[0]
        user = [user for user in self.config.users
                if username == user['name']][0]
        tenants_names = [user['tenant']]
        for role in roles_to_create:
            try:
                self.dst_cloud.keystoneclient.roles.create(name=role['role'])
            except ks_exceptions.Conflict as e:
                print "There was an error during role creating on dst: {}"\
                    .format(e)
                continue
            if role['tenant'] not in tenants_names:
                tenants_names.append(role['tenant'])

        tenants_to_create = [t for t in self.config.tenants
                             if t['name'] in tenants_names and
                             not self.dst_cloud.tenant_exists(t['name'])]
        self.dst_cloud.create_tenants(tenants_to_create)
        self.dst_cloud.create_users([user])
        self.dst_cloud.create_user_tenant_roles([user_tenant_role])

    def create_volumes_from_images(self):
        self.create_cinder_volumes(self.config.cinder_volumes_from_images)

    def boot_vms_from_volumes(self):
        for vm in self.config.vms_from_volumes:
            params = self._get_parameters_for_vm_creating(vm)
            params['block_device_mapping_v2'] = []
            params['block_device_mapping_v2'].append(
                {'source_type': 'volume',
                 'delete_on_termination': False,
                 'boot_index': 0,
                 'uuid': self.get_volume_id(vm['volume']),
                 'destination_type': 'volume'}
            )
            self.novaclient.servers.create(**params)

    def break_vm(self):
        """ Method delete vm via virsh to emulate situation, when vm is valid
        and active in nova db, but in fact does not exist
        """
        vms_to_break = []
        for vm in self.migration_utils.get_all_vms_from_config():
            if vm.get('broken'):
                vms_to_break.append(self.get_vm_id(vm['name']))

        for vm in vms_to_break:
            inst_name = getattr(self.novaclient.servers.get(vm),
                                'OS-EXT-SRV-ATTR:instance_name')
            cmd = 'virsh destroy {0} && virsh undefine {0}'.format(inst_name)
            self.migration_utils.execute_command_on_vm(
                self.get_vagrant_vm_ip(), cmd, username='root', password='')

    def break_image(self):
        all_images = self.migration_utils.get_all_images_from_config()
        images_to_break = [image for image in all_images
                           if image.get('broken')]
        for image in images_to_break:
            image_id = self.get_image_id(image['name'])
            cmd = 'rm -rf /var/lib/glance/images/%s' % image_id
            self.migration_utils.execute_command_on_vm(
                self.get_vagrant_vm_ip(), cmd, username='root', password='')

    def create_network_with_segm_id(self):
        if not self.dst_cloud:
            self.dst_cloud = Prerequisites(
                cloud_prefix='DST',
                configuration_ini=self.configuration_ini,
                config=self.config)
        self.dst_cloud.switch_user(user=self.dst_cloud.username,
                                   password=self.dst_cloud.password,
                                   tenant=self.dst_cloud.tenant)

        self.dst_cloud.create_networks(self.config.dst_networks)

    def run_preparation_scenario(self):
        print('>>> Creating tenants:')
        self.create_tenants()
        print('>>> Creating users:')
        self.create_users()
        print('>>> Creating roles:')
        self.create_roles()
        print('>>> Creating keypairs:')
        self.create_keypairs()
        print('>>> Modifying quotas:')
        self.modify_quotas()
        print('>>> Creating flavors:')
        self.create_flavors()
        print('>>> Uploading images:')
        self.upload_image()
        print('>>> Creating networking:')
        self.create_all_networking()
        if self.openstack_release in ['icehouse', 'juno']:
            print('>>> Creating server groups')
            self.create_server_groups()
            print('>>> Create bootable volume from image')
            self.create_volumes_from_images()
            print('>>> Boot vm from volume')
            self.boot_vms_from_volumes()
        print('>>> Creating vms:')
        self.create_vms()
        print('>>> Breaking VMs:')
        self.break_vm()
        print('>>> Breaking Images:')
        self.break_image()
        print('>>> Updating filtering:')
        self.update_filtering_file()
        print('>>> Creating vm snapshots:')
        self.create_vm_snapshots()
        print('>>> Creating security groups:')
        self.create_security_groups()
        print('>>> Creating cinder objects:')
        self.create_cinder_objects()
        print('>>> Writing data into the volumes:')
        self.write_data_to_volumes()
        print('>>> Creating invalid cinder objects:')
        self.create_invalid_cinder_objects()
        print('>>> Emulating vm states:')
        self.emulate_vm_states()
        print('>>> Generating vm states list:')
        self.generate_vm_state_list()
        print('>>> Deleting flavor:')
        self.delete_flavor()
        print('>>> Modifying admin tenant quotas:')
        self.modify_admin_tenant_quotas()
        print('>>> Update network quotas:')
        self.update_network_quotas()
        print('>>> Change admin role in tenants:')
        self.change_admin_role_in_tenants()
        print('>>> Creating user tenant roles:')
        self.create_user_tenant_roles()
        print('>>> Delete users which should be deleted:')
        self.delete_users()
        print('>>> Delete tenants which should be deleted:')
        self.delete_tenants()
        print('>>> Create tenant on dst, without security group:')
        self.create_tenant_wo_sec_group_on_dst()
        print('>>> Create role on dst:')
        self.create_user_on_dst()
        print('>>> Creating networks on dst:')
        self.create_network_with_segm_id()


class CleanEnv(BasePrerequisites):

    def clean_vms(self):
        vms = self.migration_utils.get_all_vms_from_config()
        vms_names = [vm['name'] for vm in vms]
        vms = self.novaclient.servers.list(search_opts={'all_tenants': 1})
        vms_ids = []
        for vm in vms:
            if vm.name not in vms_names:
                continue
            vms_ids.append(vm.id)
            self.novaclient.servers.delete(vm.id)
            print('VM "%s" has been deleted' % vm.name)
        self.wait_vms_deleted()

    def wait_vms_deleted(self, tenant_id=None):
        search_opts = {'all_tenants': 1}
        if tenant_id is not None:
            search_opts['tenant_id'] = tenant_id
        timeout = 120
        for _ in range(timeout):
            servers = self.novaclient.servers.list(
                search_opts=search_opts)
            if not servers:
                break
            for server in servers:
                if server.status != 'DELETED':
                    time.sleep(1)
                try:
                    self.novaclient.servers.delete(server.id)
                except nv_exceptions.NotFound:
                    pass
        else:
            raise RuntimeError('Next vms were not deleted')

    def clean_volumes(self):
        volumes = self.config.cinder_volumes
        volumes += itertools.chain(*[tenant['cinder_volumes'] for tenant
                                     in self.config.tenants if 'cinder_volumes'
                                     in tenant])
        for volume in self.config.cinder_volumes_from_images:
            volumes.append(volume)
        volumes_names = [volume['display_name'] for volume in volumes]
        volumes = self.cinderclient.volumes.list(
            search_opts={'all_tenants': 1})
        for volume in volumes:
            if volume.display_name not in volumes_names:
                continue
            self.cinderclient.volumes.delete(
                self.get_volume_id(volume.display_name))
            print('Volume "%s" has been deleted' % volume.display_name)

    def clean_flavors(self):
        flavors_names = [flavor['name'] for flavor in self.config.flavors]
        for tenant in self.config.tenants:
            if not tenant.get('flavors'):
                continue
            flavors_names.extend([f['name'] for f in tenant['flavors']])
        for flavor in self.novaclient.flavors.list(is_public=None):
            if flavor.name not in flavors_names:
                continue
            self.novaclient.flavors.delete(self.get_flavor_id(flavor.name))
            print('Flavor "%s" has been deleted' % flavor.name)

    def clean_images(self):
        all_images = self.migration_utils.get_all_images_from_config()
        images_names = [image['name'] for image in all_images]

        for image in self.glanceclient.images.list():
            if image.name not in images_names:
                continue
            self.glanceclient.images.delete(self.get_image_id(image.name))
            print('Image "%s" has been deleted' % image.name)

    def clean_snapshots(self):
        snaps_names = [snapshot['image_name']
                       for snapshot in self.config.snapshots]
        for snapshot in self.glanceclient.images.list():
            if snapshot.name not in snaps_names:
                continue
            self.glanceclient.images.delete(
                self.get_image_id(snapshot.name))
            print('Snapshot "%s" has been deleted' % snapshot.name)

    def clean_networks(self):
        nets = self.config.networks
        nets += itertools.chain(*[tenant['networks'] for tenant
                                  in self.config.tenants
                                  if tenant.get('networks')])
        nets_names = [net['name'] for net in nets]
        for network in self.neutronclient.list_networks()['networks']:
            if network['name'] not in nets_names:
                continue
            net_id = self.get_net_id(network['name'])
            self.clean_network_ports(net_id)
            self.neutronclient.delete_network(net_id)
            print('Network "%s" has been deleted' % network['name'])

    def delete_port(self, port):
        port_owner = port['device_owner']
        if port_owner == 'network:router_gateway':
            self.neutronclient.remove_gateway_router(port['device_id'])
        elif port_owner == 'network:router_interface':
            self.neutronclient.remove_interface_router(
                port['device_id'], {'port_id': port['id']})
        elif port_owner == 'network:dhcp' or not port_owner:
            self.neutronclient.delete_port(port['id'])
        elif 'LOADBALANCER' in port_owner:
            vips = self.neutronclient.list_vips(port_id=port['id'])['vips']
            for vip in vips:
                self.neutronclient.delete_vip(vip['id'])
        else:
            msg = 'Unknown port owner %s'
            raise RuntimeError(msg % port['device_owner'])

    def clean_network_ports(self, net_id):
        ports = self.neutronclient.list_ports(network_id=net_id)['ports']
        for port in ports:
            self.delete_port(port)

    def clean_router_ports(self, router_id):
        ports = self.neutronclient.list_ports(device_id=router_id)['ports']
        for port in ports:
            self.delete_port(port)

    def clean_routers(self):
        router_names = [router['router']['name']
                        for router in self.config.routers]
        for tenant in self.config.tenants:
            if tenant.get('routers'):
                for router in tenant['routers']:
                    router_names.append(router['router']['name'])
        for router in self.neutronclient.list_routers()['routers']:
            if router['name'] not in router_names:
                continue
            router_id = self.get_router_id(router['name'])
            self.clean_router_ports(router_id)
            self.neutronclient.delete_router(router_id)
            print('Router "%s" has been deleted' % router['name'])

    def clean_fips(self):
        floatingips = self.neutronclient.list_floatingips()['floatingips']
        for ip in floatingips:
            try:
                self.neutronclient.delete_floatingip(ip['id'])
            except nt_exceptions.NeutronClientException as e:
                print "Ip %s failed to delete: %s" % (
                    ip['floating_ip_address'], repr(e))

    def clean_security_groups(self):
        sgs = self.neutronclient.list_security_groups()['security_groups']
        for sg in sgs:
            try:
                self.neutronclient.delete_security_group(self.get_sg_id(
                    sg['name']))
            except (nt_exceptions.NeutronClientException,
                    NotFound) as e:
                print "Security group %s failed to delete: %s" % (sg['name'],
                                                                  repr(e))

    def clean_roles(self):
        roles_names = [role['name'] for role in self.config.roles]
        for role in self.keystoneclient.roles.list():
            if role.name not in roles_names:
                continue
            self.keystoneclient.roles.delete(self.get_role_id(role.name))
            print('Role "%s" has been deleted' % role.name)

    def clean_keypairs(self):
        def delete_user_keypairs(_user):
            if not _user.get('enabled'):
                return
            try:
                self.switch_user(user=_user['name'], tenant=_user['tenant'],
                                 password=_user['password'])
            except ks_exceptions.Unauthorized:
                return

            keypairs = [k.id for k in self.novaclient.keypairs.list()]
            if keypairs:
                for key_pair in keypairs:
                    self.novaclient.keypairs.delete(key_pair)
            self.switch_user(user=self.username, password=self.password,
                             tenant=self.tenant)

        for user in self.config.users:
            delete_user_keypairs(user)

    def clean_users(self):
        users_names = [user['name'] for user in self.config.users]
        for user in self.keystoneclient.users.list():
            if user.name not in users_names:
                continue
            self.keystoneclient.users.delete(self.get_user_id(user.name))
            print('User "%s" has been deleted' % user.name)

    def clean_tenants(self):
        tenants_names = [tenant['name'] for tenant in self.config.tenants]
        for tenant in self.keystoneclient.tenants.list():
            if tenant.name not in tenants_names:
                continue
            self.keystoneclient.tenants.delete(self.get_tenant_id(tenant.name))
            print('Tenant "%s" has been deleted' % tenant.name)

    def clean_cinder_snapshots(self):
        snapshots = self.config.cinder_snapshots
        snapshots += itertools.chain(
            *[tenant['cinder_snapshots'] for tenant in self.config.tenants
              if 'cinder_snapshots' in tenant])
        sn_names = [snapshot['name'] for snapshot in snapshots]
        snapshots = self.cinderclient.volume_snapshots.list(
            search_opts={'all_tenants': 1})
        for snapshot in snapshots:
            if snapshot.name not in sn_names:
                continue
            self.cinderclient.volume_snapshots.delete(
                self.get_volume_snapshot_id(snapshot.display_name))
            print('Snapshot "%s" has been deleted' % snapshot.display_name)

    def clean_namespaces(self):
        ip_addr = self.get_vagrant_vm_ip()
        if self.openstack_release == 'grizzly':
            cmd = 'quantum-netns-cleanup'
        else:
            cmd = 'neutron-netns-cleanup'
        self.migration_utils.execute_command_on_vm(ip_addr, cmd, 'root')

    def clean_lb_pools(self):
        pools = getattr(self.config, 'pools', [])
        pools += itertools.chain(
            *[tenant['pools'] for tenant in self.config.tenants
              if 'pools' in tenant])
        pools_names = [pool['name'] for pool in pools]
        for pool in self.neutronclient.list_pools()['pools']:
            if pool['name'] in pools_names:
                self.neutronclient.delete_pool(pool['id'])
                print('LBaaS pool "%s" has been deleted' % pool['name'])

    def clean_lb_vips(self):
        vips = getattr(self.config, 'vips', [])
        vips += itertools.chain(
            *[tenant['vips'] for tenant in self.config.tenants
              if 'vips' in tenant])
        vips_names = [vip['name'] for vip in vips]
        for vip in self.neutronclient.list_vips()['vips']:
            if vip['name'] in vips_names:
                self.neutronclient.delete_vip(vip['id'])
                print('LBaaS vip "%s" has been deleted' % vip['name'])

    def clean_lb_members(self):
        members = getattr(self.config, 'lb_members', [])
        members += itertools.chain(
            *[tenant['members'] for tenant in self.config.tenants
              if 'members' in tenant])
        member_address = [member['address'] for member in members]
        for member in self.neutronclient.list_members()['members']:
            if member['address'] in member_address:
                self.neutronclient.delete_member(member['id'])
                msg = 'LBaaS member for tenant "%s" has been deleted'
                print(msg % member['tenant_id'])

    def clean_lbaas_health_monitors(self):
        def check_tenant(tenant_id):
            try:
                self.keystoneclient.tenants.get(tenant_id)
                return True
            except ks_exceptions.NotFound:
                return False

        tenants_ids = [self.get_tenant_id(tenant['name'])
                       for tenant in self.config.tenants
                       if self.tenant_exists(tenant['name'])]
        tenants_ids.append(self.get_tenant_id(self.tenant))
        for hm in self.neutronclient.list_health_monitors()['health_monitors']:
            if hm['tenant_id'] in tenants_ids or not check_tenant(
                    hm['tenant_id']):
                self.neutronclient.delete_health_monitor(hm['id'])
                msg = 'LBaaS health monitor for tenant "%s" has been deleted'
                print(msg % hm['tenant_id'])

    def clean_server_groups(self):
        server_group_from_config = self.config.server_groups
        for tenant in self.config.tenants:
            if tenant.get('server_groups'):
                server_group_from_config.extend(tenant['server_groups'])
        server_group_names = [sg['name'] for sg in server_group_from_config]
        for server_group in self.get_all_server_groups():
            if server_group.name in server_group_names:
                self.novaclient.server_groups.delete(server_group.id)

    def clean_all_networking(self):
        self.clean_fips()
        self.clean_lb_members()
        self.clean_lb_vips()
        self.clean_lb_pools()
        self.clean_lbaas_health_monitors()
        self.clean_routers()
        self.clean_networks()
        self.clean_namespaces()

    def clean_objects(self):
        self.clean_vms()
        self.clean_flavors()
        self.clean_images()
        self.clean_snapshots()
        self.clean_cinder_snapshots()
        self.clean_volumes()
        self.clean_all_networking()
        self.clean_security_groups()
        self.clean_roles()
        self.clean_keypairs()
        if self.server_groups_supported:
            self.clean_server_groups()
        self.clean_users()
        self.clean_tenants()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Script to generate load for Openstack and delete '
                    'generated objects')
    parser.add_argument('--clean', help='clean objects, described'
                                        ' in configuration.ini file',
                        action='store_true')
    parser.add_argument('--env', default='SRC',
                        help='choose cloud: SRC or DST')
    parser.add_argument('cloudsconf',
                        help='Please point configuration.ini file location')

    _args = parser.parse_args()
    confparser = ConfigParser.ConfigParser()
    confparser.read(_args.cloudsconf)
    cloudsconf = get_dict_from_config_file(confparser)

    preqs = Prerequisites(config=conf, configuration_ini=cloudsconf,
                          cloud_prefix=_args.env)
    if _args.clean:
        preqs.clean_tools.clean_objects()
    else:
        preqs.run_preparation_scenario()
