// Answers wdata + GAIN, raises irq while addressed, and shows its width on data_o.
module slave #(
    parameter int GAIN  = 1,
    parameter int OUT_W = 8
) (
    input  logic             clk_i,
    input  logic             rst_ni,
    input  logic             valid_ni,
    input  logic [7:0]       wdata_i,
    output logic [15:0]      rdata_o,
    output logic             irq_o,
    output logic [OUT_W-1:0] data_o
);
    assign rdata_o = 16'(wdata_i) + 16'(GAIN);
    assign irq_o   = !valid_ni;
    assign data_o  = '1;
endmodule
