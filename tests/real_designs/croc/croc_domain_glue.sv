// The constants croc_domain.sv takes straight from packages, as outputs of a module.
//
// IP-XACT can tie a port to a number, not to a package's constant, and the address maps and the debug
// module's hart info are structs. In the hand-written croc_domain they are written at the instance:
// `.addr_map_i ( CrocAddrMap )`, `.addr_map_i ( PeriphAddrMap )` and `.hartinfo_i ( hartinfo )`.
// This is the only logic the IP-XACT description of croc_domain adds, and it holds no logic.

module croc_domain_glue (
  output croc_pkg::addr_map_rule_t [3:0] croc_addr_map_o,
  output croc_pkg::addr_map_rule_t [7:0] periph_addr_map_o,
  output dm::hartinfo_t [0:0]            hartinfo_o
);

  assign croc_addr_map_o   = croc_pkg::CrocAddrMap;
  assign periph_addr_map_o = croc_pkg::PeriphAddrMap;

  // As HARTINFO in croc_domain.sv
  assign hartinfo_o[0] = '{
    zero1:      '0,
    nscratch:   2,
    zero0:      '0,
    dataaccess: 1'b1,
    datasize:   dm::DataCount,
    dataaddr:   dm::DataAddr
  };

endmodule
