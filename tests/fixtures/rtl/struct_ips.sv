package req_pkg;
    typedef struct packed {
        logic       valid;
        logic [7:0] data;
    } req_t;
endpackage

module src (output req_pkg::req_t req_o, output logic ok_o);
    assign req_o = '0;
    assign ok_o  = 1'b1;
endmodule

module dst (input req_pkg::req_t req_i, output logic flag_o);
    assign flag_o = req_i.valid;
endmodule
