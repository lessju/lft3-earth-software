#include <jetstream/plugin.hh>

// Exports the CyberEther plugin ABI record. The blocks and modules in this library register themselves
// statically (JST_REGISTER_BLOCK / JST_REGISTER_MODULE); CyberEther drains those registrations when it
// loads lft3.cep.
JST_REGISTER_PLUGIN();
